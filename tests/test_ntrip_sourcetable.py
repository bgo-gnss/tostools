"""Pure-function tests for tostools.ntrip_sourcetable (no network)."""

import pytest

from tostools import ntrip_sourcetable as nst

# A real 1008 (antenna descriptor + serial) frame captured from ENTC0.
ENTC0_1008 = bytes.fromhex(
    "d300203f00001454524d35373937312e303020202020204e4f4e450006303137333331" "8cc0d2"
)


def frame(msg: int, payload: bytes = b"") -> bytes:
    """Build a CRC-valid RTCM 3 frame; ``payload`` defaults to ``msg`` + zeros."""
    payload = payload or bytes([msg >> 4, (msg & 0x0F) << 4]) + b"\x00" * 6
    body = bytes([0xD3, len(payload) >> 8, len(payload) & 0xFF]) + payload
    return body + nst.crc24q(body).to_bytes(3, "big")


def eph(msg: int, sat: int) -> bytes:
    """Ephemeris frame: 12-bit msg number, then a 6-bit satellite id."""
    word = (msg << 12) | (sat << 6)
    return frame(msg, word.to_bytes(3, "big") + b"\x00" * 6)


# --- framing ---------------------------------------------------------------


def test_captured_frame_parses_and_crc_verifies():
    assert list(nst.iter_rtcm3_frames(ENTC0_1008)) == [(1008, 0, len(ENTC0_1008))]


def test_corrupt_crc_is_rejected():
    bad = ENTC0_1008[:-1] + bytes([ENTC0_1008[-1] ^ 1])
    assert list(nst.iter_rtcm3_frames(bad)) == []


def test_false_preamble_in_garbage_resyncs():
    # 0xD3 bytes that do not start a valid frame must not swallow the real one
    buf = b"\xd3\x00\x05garbage\xd3" + frame(1004) + b"\xd3\x01" + frame(1033)
    assert [m for m, _, _ in nst.iter_rtcm3_frames(buf)] == [1004, 1033]


def test_detect_format():
    assert nst.detect_format(frame(1004) * 3) == "RTCM3"
    assert (
        nst.detect_format(b"$@\x00\x01" * 10) == "UNKNOWN"
    )  # bare sync bytes are not SBF
    assert nst.detect_format(b"\x00" * 100) == "UNKNOWN"


def test_ephemeris_satellite_id():
    payload = eph(1019, 17)[3:-3]
    assert nst.ephemeris_satellite(1019, payload) == 17
    assert nst.ephemeris_satellite(1004, payload) == -1


# --- message-set interpretation -------------------------------------------


@pytest.mark.parametrize(
    "msgs, systems, carrier, version",
    [
        ({1004, 1012, 1019, 1033}, "GPS+GLO", 2, "RTCM 3.1"),
        ({1002, 1006}, "GPS", 1, "RTCM 3.0"),
        ({1075, 1085, 1095, 1125, 1230}, "GPS+GLO+GAL+BDS", 2, "RTCM 3.2"),
        ({1004, 1042, 1125}, "GPS+BDS", 2, "RTCM 3.3"),
        ({1008, 1013, 1033}, "", 0, "RTCM 3.1"),  # ENTC0's broken stream
    ],
)
def test_message_set_fields(msgs, systems, carrier, version):
    assert nst.nav_systems(msgs) == systems
    assert nst.carrier(msgs) == carrier
    assert nst.rtcm_version(msgs) == version


def test_summarise_sample_measures_intervals_not_counts():
    chunks = []
    for second in range(60):
        data = frame(1004) + frame(1012)
        if second % 10 == 0:
            data += frame(1033)
        if second in (0, 12, 24, 36, 48):  # one satellite every 12 s ...
            data += eph(1019, 5)
        if second in (1, 13, 25, 37, 49):  # ... another, one second later
            data += eph(1019, 9)
        if second == 30:
            data += frame(1006)
        chunks.append((1000.0 + second, data))
    s = nst.summarise_sample(chunks, 60.0)
    assert s.stream_format == "RTCM3"
    # a per-type gap would make 1019 look like "1 s"; per satellite it is 12
    assert s.format_details() == "1004(1),1006,1012(1),1019(12),1033(10)"
    assert s.bitrate == round(s.n_bytes * 8 / 60)


def test_frames_in_one_read_count_as_one_epoch():
    chunks = [(float(t), frame(1004) * 3) for t in range(0, 10, 2)]
    assert nst.summarise_sample(chunks, 10.0).intervals == {1004: 2}


# --- NTRIP response classification ----------------------------------------


@pytest.mark.parametrize(
    "head, status",
    [
        (b"ICY 200 OK\r\n", nst.LIVE),
        (b"SOURCETABLE 200 OK\r\nServer: x\r\n", nst.NOT_FED),
        (b"HTTP/1.1 401 Unauthorized\r\n", nst.AUTH_DENIED),
        (b"HTTP/1.1 503 Server Full\r\n", nst.ERROR),
    ],
)
def test_classify_response(head, status):
    assert nst.classify_response(head) == status


# --- sourcetable text -----------------------------------------------------


@pytest.mark.parametrize(
    "name, ident",
    [
        ("Skipastígshraun", "Skipastigshraun"),
        ("Þorbjörn Norðaustan", "Thorbjorn Nordaustan"),
        ("Gæsafjöll;x", "Gaesafjoll,x"),
    ],
)
def test_ascii_identifier(name, ident):
    assert nst.ascii_identifier(name) == ident


LIVE_TABLE = (
    "CAS;ntrcaster.vedur.is;2101;IMOcaster;IMO;0;ISL;64.13;-21.90;0.0.0.0;0;x\n"
    "NET;ISGPS;IMO;B;N;http://www.vedur.is/;skl;gps@vedur.is;none \n"
    "STR;ZZZZ0;Z;RTCM 3.1;1004(197);2;GPS;ISGPS;ISL;6r375;-20.21;0;0;"
    "TRIMBLE NETRS;none;B;N;2000\n"
    "STR;AAAA0;A;RTCM 3.1;1004(197);2;GPS;ISGPS;ISL;63.00;-19.00;0;0;"
    "TRIMBLE NETR9;none;B;N;2000\n"
)


def test_parse_render_keeps_header_verbatim_and_sorts():
    table = nst.parse_sourcetable(LIVE_TABLE)
    text = nst.render_sourcetable(table)
    lines = text.splitlines()
    assert lines[1].endswith("none ")  # trailing space on NET survives
    assert [ln.split(";")[1] for ln in lines[2:]] == ["AAAA0", "ZZZZ0"]
    assert "ENDSOURCETABLE" not in text and text.endswith("\n")


# --- merge rules ----------------------------------------------------------

STATION = nst.StationInfo("Storólfshvoll", 63.7512, -20.2101, "SEPT POLARX5")


def live(*msgs, fmt="RTCM3"):
    summary = nst.StreamSummary(fmt, 60.0, 15000, {m: 1 for m in msgs})
    return nst.ProbeResult("X", nst.LIVE, "", summary)


def fields(rec):
    return dict(zip(nst.STR_FIELDS, rec))


def test_measured_line_replaces_stream_and_tos_fields():
    prev = nst.parse_sourcetable(LIVE_TABLE).records["ZZZZ0"]
    rec, outcome = nst.build_record(
        "ZZZZ0",
        previous=prev,
        probe=live(1004, 1012, 1075),
        station=STATION,
        auth="N",
    )
    f = fields(rec)
    assert outcome == "measured"
    assert (f["latitude"], f["longitude"]) == ("63.75", "-20.21")  # 6r375 fixed
    assert f["identifier"] == "Storolfshvoll"
    assert f["generator"] == "SEPT POLARX5"
    assert f["format"] == "RTCM 3.2" and f["nav_system"] == "GPS+GLO"
    assert f["authentication"] == "N" and f["bitrate"] == "2000"


def test_live_without_observations_keeps_previous_stream_fields():
    prev = nst.parse_sourcetable(LIVE_TABLE).records["AAAA0"]
    rec, outcome = nst.build_record(
        "AAAA0",
        previous=prev,
        probe=live(1008, 1013, 1033),
        station=STATION,
        auth="B",
    )
    assert outcome == "no_obs"
    assert fields(rec)["format_details"] == "1004(197)"


def test_unmeasurable_existing_line_is_kept():
    prev = nst.parse_sourcetable(LIVE_TABLE).records["AAAA0"]
    probe = nst.ProbeResult("AAAA0", nst.NOT_FED, "SOURCETABLE 200 OK")
    rec, outcome = nst.build_record(
        "AAAA0", previous=prev, probe=probe, station=None, auth="B"
    )
    assert outcome == "kept" and rec == prev  # already-ASCII line: verbatim


@pytest.mark.parametrize(
    "probe, station",
    [
        (nst.ProbeResult("NEW0", nst.NOT_FED), STATION),  # not fed
        (nst.ProbeResult("NEW0", nst.AUTH_DENIED), STATION),  # can't measure
        (live(1004), None),  # live but no TOS station
    ],
)
def test_new_mount_is_only_added_from_a_measurement(probe, station):
    assert nst.build_record(
        "NEW0", previous=None, probe=probe, station=station, auth="B"
    ) == (None, "skipped")


def test_new_sbf_mount_is_added_as_sbf():
    rec, outcome = nst.build_record(
        "GEVK1", previous=None, probe=live(fmt="SBF"), station=STATION, auth="N"
    )
    f = fields(rec)
    assert outcome == "measured"
    assert (f["format"], f["format_details"], f["bitrate"]) == ("SBF", "", "2000")


def sbf_block(block_id: int = 4027, body: bytes = b"\x00" * 12) -> bytes:
    length = 8 + len(body)
    tail = block_id.to_bytes(2, "little") + length.to_bytes(2, "little") + body
    return b"$@" + nst.crc16_ccitt(tail).to_bytes(2, "little") + tail


def test_sbf_needs_crc_valid_blocks_not_just_sync_bytes():
    assert nst.detect_format(sbf_block() * 3) == "SBF"
    # "$@" by chance in other binary (the SELF1 RT17 false positive)
    assert nst.detect_format(b"\x02\x57$@\x10\x00\x00\x00\x03" * 10) == "UNKNOWN"
    corrupt = bytearray(sbf_block())
    corrupt[-1] ^= 1
    assert nst.count_sbf_blocks(bytes(corrupt) * 3) == 0


def test_kept_line_without_tos_station_is_made_ascii():
    prev = "STR;GRVC0;Gr\xedmsv\xf6tn;RTCM 3.1;1004(197);2;GPS;ISGPS;ISL;64.4;-17.3;0;0;X;none;B;N;2000"
    rec, outcome = nst.build_record(
        "GRVC0",
        previous=prev.split(";"),
        probe=nst.ProbeResult("GRVC0", nst.NOT_FED),
        station=None,
        auth="B",
    )
    assert outcome == "kept"
    assert fields(rec)["identifier"] == "Grimsvotn"
