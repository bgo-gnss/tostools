"""Generate the NTRIP caster sourcetable from measured streams + TOS metadata.

The IMO caster (ntrcaster.vedur.is, BKG NtripCaster 2.0) serves a hand-edited
``/etc/ntripcaster/sourcetable.dat``. It went stale: 65 ``STR`` lines while
~160 mountpoints stream, so clients that browse the table cannot find most of
the network. This module rebuilds it from two sources:

* **the stream itself** — a short NTRIP 1 client read of each mountpoint,
  parsed into RTCM 3 frames (CRC24Q-verified), gives the message list with
  update intervals, carrier, navigation systems, RTCM version and bitrate;
* **TOS** — station name, position and the current receiver model.

Membership is NOT derived from probing. Which mountpoints exist is an input
(the union of the current table, ``relay pull`` lines, ``clientmounts.aut``
and the caster log, gathered on the caster host); measurement only fills in
details. A mountpoint that is down when measured keeps its previous line and
is reported, never silently dropped.

The ``(n)`` after each message number is the NTRIP-specified **update interval
in seconds** — the median inter-arrival time measured over the sample window.
The old hand-written table carried message *counts* there, copied to every
station, so nearly every regenerated line differs from it.

Everything except :func:`probe_mountpoint` is pure and unit-tested; the CLI
lives in ``tosGPS sourcetable``. Writing the result onto the caster host is
deliberately out of scope here.
"""

from __future__ import annotations

import base64
import bisect
import configparser
import difflib
import os
import socket
import statistics
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# RTCM 3 framing
# --------------------------------------------------------------------------

RTCM3_PREAMBLE = 0xD3
_CRC24Q_POLY = 0x1864CFB


def crc24q(data: bytes) -> int:
    """CRC-24Q (Qualcomm) as used by RTCM 3 — over preamble+header+payload."""
    crc = 0
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= _CRC24Q_POLY
    return crc & 0xFFFFFF


def iter_rtcm3_frames(buf: bytes) -> Iterator[Tuple[int, int, int]]:
    """Yield ``(message_number, start_offset, end_offset)`` per valid frame.

    A ``0xD3`` byte inside a payload is common, so a candidate frame counts
    only when its CRC24Q verifies; on a failure the scan resumes one byte
    after the false preamble (resync), never at the claimed frame end.
    """
    i = 0
    n = len(buf)
    while i + 6 <= n:
        if buf[i] != RTCM3_PREAMBLE or buf[i + 1] & 0xFC:
            i += 1
            continue
        length = ((buf[i + 1] & 0x03) << 8) | buf[i + 2]
        end = i + 3 + length + 3
        if length < 2 or end > n:
            i += 1
            continue
        body = buf[i : i + 3 + length]
        if crc24q(body) != int.from_bytes(buf[end - 3 : end], "big"):
            i += 1
            continue
        msg = (buf[i + 3] << 4) | (buf[i + 4] >> 4)
        yield msg, i, end
        i = end


SBF_SYNC = b"$@"


def crc16_ccitt(data: bytes) -> int:
    """CRC-16-CCITT (poly 0x1021, init 0) as used by Septentrio SBF."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
        crc &= 0xFFFF
    return crc


def count_sbf_blocks(buf: bytes, *, limit: int = 0) -> int:
    """Count CRC-valid SBF blocks (stop early once ``limit`` is reached).

    Block: ``$@`` sync, CRC (u2 LE), ID (u2), Length (u2 LE, whole block,
    multiple of 4); the CRC covers ID through the end of the block. A bare
    ``$@`` count is not enough — the pair turns up by chance in Trimble
    RT17 binary (SELF1 was misread as SBF that way).
    """
    count = 0
    i = buf.find(SBF_SYNC)
    while i != -1 and i + 8 <= len(buf):
        length = int.from_bytes(buf[i + 6 : i + 8], "little")
        end = i + length
        if length >= 8 and length % 4 == 0 and end <= len(buf):
            crc = int.from_bytes(buf[i + 2 : i + 4], "little")
            if crc16_ccitt(buf[i + 4 : end]) == crc:
                count += 1
                if limit and count >= limit:
                    break
                i = buf.find(SBF_SYNC, end)
                continue
        i = buf.find(SBF_SYNC, i + 1)
    return count


def detect_format(buf: bytes, *, min_frames: int = 3) -> str:
    """Classify a stream sample as ``"RTCM3"``, ``"SBF"`` or ``"UNKNOWN"``.

    Both need ``min_frames`` CRC-valid frames/blocks. HRIC1 / GEVK1 push raw
    SBF and must get an ``SBF`` line rather than an empty RTCM one; Trimble
    RT17/RT27 raw streams come out ``UNKNOWN`` (their previous line is kept).
    """
    frames = 0
    for _ in iter_rtcm3_frames(buf):
        frames += 1
        if frames >= min_frames:
            return "RTCM3"
    if count_sbf_blocks(buf, limit=min_frames) >= min_frames:
        return "SBF"
    return "UNKNOWN"


# --------------------------------------------------------------------------
# Message-set interpretation
# --------------------------------------------------------------------------

# Observation messages per navigation system (legacy + MSM1-7).
_OBS_SYSTEMS: Dict[str, range | Tuple[int, ...]] = {
    "GPS": (1001, 1002, 1003, 1004, *range(1071, 1078)),
    "GLO": (1009, 1010, 1011, 1012, *range(1081, 1088)),
    "GAL": tuple(range(1091, 1098)),
    "SBAS": tuple(range(1101, 1108)),
    "QZS": tuple(range(1111, 1118)),
    "BDS": tuple(range(1121, 1128)),
}
_SYSTEM_ORDER = ("GPS", "GLO", "GAL", "BDS", "QZS", "SBAS")
_L1_ONLY = {1001, 1002, 1009, 1010}
_DUAL = {1003, 1004, 1011, 1012}
_MSM = set(range(1071, 1128))


def nav_systems(messages: Iterable[int]) -> str:
    """``GPS+GLO+GAL+BDS``-style system list from the observation messages."""
    present = set(messages)
    systems = [s for s in _SYSTEM_ORDER if present.intersection(_OBS_SYSTEMS[s])]
    return "+".join(systems)


def carrier(messages: Iterable[int]) -> int:
    """NTRIP carrier field: 0 none, 1 L1, 2 L1+L2.

    MSM is counted as dual-frequency: every receiver in this network that
    emits MSM tracks at least L1+L2, and the signal mask is not decoded.
    """
    present = set(messages)
    if present & (_DUAL | _MSM):
        return 2
    if present & _L1_ONLY:
        return 1
    return 0


def rtcm_version(messages: Iterable[int]) -> str:
    """Lowest RTCM 3.x revision that defines every message seen."""
    present = set(messages)
    if present & {1041, 1042, 1044}:
        return "RTCM 3.3"
    if present & (_MSM | {1045, 1046, 1230}):
        return "RTCM 3.2"
    if present & ({1019, 1020} | set(range(1029, 1040))):
        return "RTCM 3.1"
    return "RTCM 3.0"


@dataclass
class StreamSummary:
    """What one measured sample says about a mountpoint."""

    stream_format: str  # RTCM3 / SBF / UNKNOWN
    duration_s: float
    n_bytes: int
    intervals: Dict[int, Optional[int]] = field(default_factory=dict)

    @property
    def bitrate(self) -> int:
        if self.duration_s <= 0:
            return 0
        return int(round(self.n_bytes * 8 / self.duration_s))

    @property
    def messages(self) -> List[int]:
        return sorted(self.intervals)

    def format_details(self) -> str:
        """``1004(1),1006(10),1033`` — interval omitted when seen only once."""
        parts = []
        for msg in self.messages:
            iv = self.intervals[msg]
            parts.append(f"{msg}" if iv is None else f"{msg}({iv})")
        return ",".join(parts)


def summarise_sample(
    chunks: Sequence[Tuple[float, bytes]], duration_s: Optional[float] = None
) -> StreamSummary:
    """Reduce timestamped socket reads to a :class:`StreamSummary`.

    Each frame is stamped with the arrival time of the read that completed
    it. The interval per message type is the median gap between consecutive
    *distinct* arrival epochs, rounded to whole seconds (minimum 1) —
    several frames of one type in a single read are one epoch, not several.
    """
    buf = b"".join(c for _, c in chunks)
    if duration_s is None:
        duration_s = (chunks[-1][0] - chunks[0][0]) if len(chunks) > 1 else 0.0
    fmt = detect_format(buf)
    summary = StreamSummary(fmt, duration_s, len(buf))
    if fmt != "RTCM3":
        return summary

    ends: List[int] = []
    times: List[float] = []
    total = 0
    for t, c in chunks:
        total += len(c)
        ends.append(total)
        times.append(t)

    # Ephemerides go one satellite per message, back to back, so a per-type
    # gap would read "1 s". They are keyed by satellite instead and the
    # interval is the median of the per-satellite repeat times.
    arrivals: Dict[Tuple[int, int], List[float]] = {}
    for msg, start, end in iter_rtcm3_frames(buf):
        t = times[min(bisect.bisect_left(ends, end), len(times) - 1)]
        key = (msg, ephemeris_satellite(msg, buf[start + 3 : end - 3]))
        seen = arrivals.setdefault(key, [])
        if not seen or t - seen[-1] > 0.2:
            seen.append(t)

    gaps: Dict[int, List[float]] = {}
    for (msg, _sat), ts in arrivals.items():
        per_key = gaps.setdefault(msg, [])
        if len(ts) >= 2:
            per_key.append(statistics.median(b - a for a, b in zip(ts, ts[1:])))
    for msg, values in gaps.items():
        summary.intervals[msg] = (
            max(1, int(round(statistics.median(values)))) if values else None
        )
    return summary


_EPHEMERIS_SAT_BITS = {1019: 6, 1020: 6, 1042: 6, 1044: 4, 1045: 6, 1046: 6}


def ephemeris_satellite(msg: int, payload: bytes) -> int:
    """Satellite id of an ephemeris message (the field after the 12-bit
    message number), or ``-1`` for every other message type."""
    bits = _EPHEMERIS_SAT_BITS.get(msg)
    if bits is None or len(payload) < 3:
        return -1
    word = int.from_bytes(payload[:3], "big")  # 24 bits: msg(12) + sat(..)
    return (word >> (12 - bits)) & ((1 << bits) - 1)


# --------------------------------------------------------------------------
# NTRIP 1 client probe (the only I/O in this module)
# --------------------------------------------------------------------------

LIVE, NOT_FED, AUTH_DENIED, ERROR = "live", "not_fed", "auth_denied", "error"


@dataclass
class ProbeResult:
    mount: str
    status: str
    detail: str = ""
    summary: Optional[StreamSummary] = None


def classify_response(head: bytes) -> str:
    """Map the caster's first response line onto a probe status.

    ``ICY 200`` is a live stream; a ``SOURCETABLE 200`` reply means the
    mountpoint is not being fed; a 401 means *our* login is not in the
    mount's group — which says nothing about whether the stream is up, and
    must never be reported as down.
    """
    first = head.split(b"\r\n", 1)[0].split(b"\n", 1)[0].strip().upper()
    if first.startswith(b"ICY 200"):
        return LIVE
    if first.startswith(b"SOURCETABLE"):
        return NOT_FED
    if b" 401" in first:
        return AUTH_DENIED
    return ERROR


def probe_mountpoint(
    host: str,
    port: int,
    mount: str,
    user: str,
    password: str,
    *,
    duration_s: float = 60.0,
    connect_timeout: float = 10.0,
    max_bytes: int = 8 * 1024 * 1024,
) -> ProbeResult:
    """Read ``duration_s`` seconds of one mountpoint over NTRIP 1.

    NTRIP 1 (no ``Ntrip-Version`` header) is used on purpose: the caster
    answers with a bare ``ICY 200 OK`` and an unchunked byte stream.
    """
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    request = (
        f"GET /{mount} HTTP/1.0\r\n"
        "User-Agent: NTRIP tostools-sourcetable/1.0\r\n"
        f"Authorization: Basic {token}\r\n\r\n"
    ).encode()
    try:
        sock = socket.create_connection((host, port), timeout=connect_timeout)
    except OSError as exc:
        return ProbeResult(mount, ERROR, f"connect: {exc}")
    with sock:
        try:
            sock.sendall(request)
            sock.settimeout(connect_timeout)
            head = b""
            while b"\r\n" not in head and len(head) < 4096:
                part = sock.recv(4096)
                if not part:
                    break
                head += part
        except OSError as exc:
            return ProbeResult(mount, ERROR, f"request: {exc}")
        status = classify_response(head)
        if status != LIVE:
            line = head.split(b"\r\n", 1)[0][:80].decode("latin-1", "replace")
            return ProbeResult(mount, status, line)

        body = head.split(b"\r\n", 1)[1] if b"\r\n" in head else b""
        if body.startswith(b"\r\n"):
            body = body[2:]
        start = time.monotonic()
        chunks: List[Tuple[float, bytes]] = [(start, body)] if body else []
        total = len(body)
        deadline = start + duration_s
        sock.settimeout(5.0)
        while total < max_bytes:
            now = time.monotonic()
            if now >= deadline:
                break
            sock.settimeout(min(5.0, max(0.1, deadline - now)))
            try:
                part = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError as exc:
                return ProbeResult(mount, ERROR, f"read: {exc}")
            if not part:
                break
            chunks.append((time.monotonic(), part))
            total += len(part)
    elapsed = time.monotonic() - start
    if not chunks:
        return ProbeResult(mount, NOT_FED, "ICY 200 but no data")
    return ProbeResult(mount, LIVE, "", summarise_sample(chunks, elapsed))


def load_caster_credentials(
    path: Optional[Path] = None,
) -> Tuple[str, int, str, str]:
    """``(host, port, user, password)`` from ``receivers.cfg [ntrip_defaults]``.

    The same login the receivers health probe uses. ``NTRIP_USER`` /
    ``NTRIP_PASSWORD`` override the file. The password is never logged.
    """
    if path is None:
        base = os.environ.get("GPS_CONFIG_PATH", str(Path.home() / ".config/gpsconfig"))
        path = Path(base) / "receivers.cfg"
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(path)
    sec = cfg["ntrip_defaults"] if cfg.has_section("ntrip_defaults") else {}
    host = sec.get("host", "ntrcaster.vedur.is")
    port = int(sec.get("port", "2101"))
    user = os.environ.get("NTRIP_USER", sec.get("username", ""))
    password = os.environ.get("NTRIP_PASSWORD", sec.get("password", ""))
    return host, port, user, password


# --------------------------------------------------------------------------
# Sourcetable records
# --------------------------------------------------------------------------

STR_FIELDS = (
    "type",
    "mountpoint",
    "identifier",
    "format",
    "format_details",
    "carrier",
    "nav_system",
    "network",
    "country",
    "latitude",
    "longitude",
    "nmea",
    "solution",
    "generator",
    "compression",
    "authentication",
    "fee",
    "bitrate",
)

_TRANSLIT = str.maketrans(
    {
        "þ": "th",
        "Þ": "Th",
        "ð": "d",
        "Ð": "D",
        "æ": "ae",
        "Æ": "Ae",
        "ö": "o",
        "Ö": "O",
        "ø": "o",
        "Ø": "O",
        "ß": "ss",
    }
)


def ascii_identifier(name: str) -> str:
    """Icelandic station name → plain-ASCII sourcetable identifier.

    ``Skipastígshraun`` → ``Skipastigshraun``, ``Þeistareykir`` →
    ``Theistareykir``. ``;`` is the field separator and is replaced.
    """
    out = name.translate(_TRANSLIT)
    out = unicodedata.normalize("NFKD", out).encode("ascii", "ignore").decode()
    return out.replace(";", ",").strip()


@dataclass
class Sourcetable:
    header: List[str]  # non-STR lines, verbatim (incl. trailing spaces)
    records: Dict[str, List[str]]  # mountpoint -> STR fields


def parse_sourcetable(text: str) -> Sourcetable:
    header: List[str] = []
    records: Dict[str, List[str]] = {}
    for line in text.splitlines():
        if line.startswith("STR;"):
            fields = line.split(";")
            records[fields[1]] = fields
        elif line.strip() and line.strip() != "ENDSOURCETABLE":
            header.append(line)
    return Sourcetable(header, records)


def render_sourcetable(table: Sourcetable) -> str:
    """Header lines verbatim, then STR lines sorted by mountpoint, LF-ended.

    No ``ENDSOURCETABLE`` — the caster appends it when serving the file.
    """
    lines = list(table.header)
    lines += [";".join(table.records[m]) for m in sorted(table.records)]
    return "\n".join(lines) + "\n"


@dataclass
class StationInfo:
    """The TOS-derived slice of an STR line."""

    name: str
    lat: float
    lon: float
    receiver_model: str = ""


DEFAULTS = {
    "network": "ISGPS",
    "country": "ISL",
    "nmea": "0",
    "solution": "0",
    "compression": "none",
    "fee": "N",
}


def build_record(
    mount: str,
    *,
    previous: Optional[List[str]],
    probe: Optional[ProbeResult],
    station: Optional[StationInfo],
    auth: str,
) -> Tuple[Optional[List[str]], str]:
    """Merge one mountpoint into STR fields; returns ``(fields, outcome)``.

    Outcomes:

    * ``measured`` — a usable live sample (RTCM 3 with observations, or SBF)
      supplied the stream fields;
    * ``no_obs`` — live RTCM 3 carrying no observation messages at all (a
      receiver fault such as ENTC0 sending only 1008/1013/1033). The stream
      is broken, not re-described: the previous line is kept;
    * ``kept`` — not measurable (not fed, auth denied, error); previous line
      kept with its TOS fields refreshed;
    * ``skipped`` — nothing honest to write: a new mount that could not be
      measured, or one with neither a previous line nor a TOS station.

    A mount is therefore only ever *added* from a measurement, and only ever
    *removed* by the caller. ``auth`` is ``B`` when the mount is listed in
    clientmounts.aut (verified: unlisted mounts are served without login).
    """
    summary = probe.summary if probe is not None else None
    usable = summary is not None and (
        summary.stream_format == "SBF"
        or (summary.stream_format == "RTCM3" and nav_systems(summary.messages))
    )
    degraded = summary is not None and summary.stream_format == "RTCM3" and not usable
    if not previous and (not usable or station is None):
        return None, "skipped"

    rec = dict(zip(STR_FIELDS, previous)) if previous else {}
    rec["type"] = "STR"
    rec["mountpoint"] = mount
    for key, value in DEFAULTS.items():
        rec.setdefault(key, value)
    if station is not None:
        rec["identifier"] = ascii_identifier(station.name)
        rec["latitude"] = f"{station.lat:.2f}"
        rec["longitude"] = f"{station.lon:.2f}"
        if station.receiver_model:
            rec["generator"] = station.receiver_model
    rec["authentication"] = auth

    if usable and summary is not None and summary.stream_format == "RTCM3":
        msgs = summary.messages
        rec["format"] = rtcm_version(msgs)
        rec["format_details"] = summary.format_details()
        rec["carrier"] = str(carrier(msgs))
        rec["nav_system"] = nav_systems(msgs)
        rec["bitrate"] = str(summary.bitrate)
        outcome = "measured"
    elif usable and summary is not None:
        rec["format"] = "SBF"
        rec["format_details"] = ""
        rec["carrier"] = "2"
        rec.setdefault("nav_system", "")
        rec["bitrate"] = str(summary.bitrate)
        outcome = "measured"
    else:
        outcome = "no_obs" if degraded else "kept"
    return [rec.get(k, "") for k in STR_FIELDS], outcome


def unified_diff(old: str, new: str, *, label: str = "sourcetable.dat") -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"{label} (live, sorted)",
            tofile=f"{label} (generated)",
        )
    )
