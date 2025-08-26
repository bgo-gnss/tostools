#!/usr/bin/env python3
"""
TOS Data Quality Issue Management System

This module provides comprehensive tracking and reporting of data quality issues
encountered when processing GPS station metadata from the TOS database.

Purpose: Transform GPS processing system into data quality monitoring system
that helps improve the entire TOS ecosystem while maintaining operational reliability.
"""

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from .logging import get_logger


class IssueType(Enum):
    """Classification of TOS data quality issues"""

    MISSING_MONUMENT = "missing_monument_data"
    INCOMPLETE_ANTENNA = "incomplete_antenna_data"
    INVALID_COORDINATES = "invalid_coordinate_data"
    MISSING_RECEIVER = "missing_receiver_data"
    MISSING_FIRMWARE = "missing_firmware_version"
    INVALID_DATE_RANGE = "invalid_date_range"
    MISSING_CONTACT_INFO = "missing_contact_information"
    INCOMPLETE_DEVICE_HISTORY = "incomplete_device_history"
    UNKNOWN_ANTENNA_TYPE = "unknown_antenna_type"
    MISSING_SERIAL_NUMBERS = "missing_serial_numbers"


class IssueSeverity(Enum):
    """Impact severity classification"""

    CRITICAL = "critical"  # Processing fails completely
    WARNING = "warning"  # Processing continues with degraded output
    INFO = "info"  # Processing continues with minor impact


@dataclass
class TOSDataIssue:
    """Structured representation of a TOS data quality issue"""

    station: str
    session_start: Optional[str] = None
    session_end: Optional[str] = None
    issue_type: str = ""
    severity: str = IssueSeverity.WARNING.value
    description: str = ""
    impact: str = ""
    fallback_used: str = ""
    tos_entity_id: Optional[int] = None
    detected_at: str = ""
    context: Dict[str, Any] = None

    def __post_init__(self):
        if not self.detected_at:
            self.detected_at = datetime.now().isoformat()
        if self.context is None:
            self.context = {}


class DataQualityManager:
    """
    Central manager for TOS data quality issue tracking and reporting

    Collects issues during GPS processing, provides graceful fallbacks,
    and generates reports for TOS database improvement.
    """

    def __init__(self):
        self.issues: List[TOSDataIssue] = []
        self.logger = get_logger(__name__)
        self._session_cache = {}  # Cache for issue deduplication

    def report_issue(
        self,
        station: str,
        issue_type: IssueType,
        severity: IssueSeverity = IssueSeverity.WARNING,
        description: str = "",
        impact: str = "",
        fallback_used: str = "",
        session_start: Optional[str] = None,
        session_end: Optional[str] = None,
        tos_entity_id: Optional[int] = None,
        context: Optional[Dict] = None,
    ) -> TOSDataIssue:
        """
        Report a TOS data quality issue encountered during processing

        Args:
            station: Station marker (e.g., 'REYK', 'RHOF')
            issue_type: Classification of the issue
            severity: Impact level on processing
            description: Human-readable description
            impact: What functionality is affected
            fallback_used: How the system handled the issue
            session_start: Start date of affected session
            session_end: End date of affected session
            tos_entity_id: TOS database entity ID for tracking
            context: Additional debug information

        Returns:
            TOSDataIssue object for further processing
        """
        issue = TOSDataIssue(
            station=station,
            session_start=session_start,
            session_end=session_end,
            issue_type=issue_type.value,
            severity=severity.value,
            description=description,
            impact=impact,
            fallback_used=fallback_used,
            tos_entity_id=tos_entity_id,
            context=context or {},
        )

        # Check for duplicates to avoid spam
        issue_key = f"{station}_{issue_type.value}_{session_start}"
        if issue_key not in self._session_cache:
            self.issues.append(issue)
            self._session_cache[issue_key] = True

            # Log based on severity
            log_msg = f"TOS Data Quality Issue - {station}: {description}"
            if severity == IssueSeverity.CRITICAL:
                self.logger.error(log_msg)
            elif severity == IssueSeverity.WARNING:
                self.logger.warning(log_msg)
            else:
                self.logger.info(log_msg)

        return issue

    def get_monument_height_safe(self, device_session: Dict, station: str) -> float:
        """
        Safely extract monument height with issue reporting

        This addresses the KeyError: 'monument' issue we encountered with REYK station.
        """
        try:
            return float(device_session["monument"]["monument_height"])
        except KeyError:
            self.report_issue(
                station=station,
                issue_type=IssueType.MISSING_MONUMENT,
                severity=IssueSeverity.WARNING,
                description="Monument data missing from device session",
                impact="Antenna height calculation degraded - using antenna height only",
                fallback_used="monument_height = 0.0",
                session_start=(
                    device_session.get("time_from", "").strftime("%Y-%m-%d")
                    if device_session.get("time_from")
                    else None
                ),
                session_end=(
                    device_session.get("time_to", "").strftime("%Y-%m-%d")
                    if device_session.get("time_to")
                    else None
                ),
                context={"available_keys": list(device_session.keys())},
            )
            return 0.0
        except (ValueError, TypeError) as e:
            self.report_issue(
                station=station,
                issue_type=IssueType.INVALID_COORDINATES,
                severity=IssueSeverity.WARNING,
                description=f"Invalid monument height value: {str(e)}",
                impact="Antenna height calculation degraded",
                fallback_used="monument_height = 0.0",
                session_start=(
                    device_session.get("time_from", "").strftime("%Y-%m-%d")
                    if device_session.get("time_from")
                    else None
                ),
                context={"monument_data": device_session.get("monument", {})},
            )
            return 0.0

    def get_antenna_height_safe(self, device_session: Dict, station: str) -> float:
        """
        Safely extract antenna height with issue reporting
        """
        try:
            return float(device_session["antenna"]["antenna_height"])
        except KeyError:
            self.report_issue(
                station=station,
                issue_type=IssueType.INCOMPLETE_ANTENNA,
                severity=IssueSeverity.WARNING,
                description="Antenna height missing from device session",
                impact="Total antenna height calculation impossible",
                fallback_used="antenna_height = 0.0",
                session_start=(
                    device_session.get("time_from", "").strftime("%Y-%m-%d")
                    if device_session.get("time_from")
                    else None
                ),
                context={"antenna_data": device_session.get("antenna", {})},
            )
            return 0.0
        except (ValueError, TypeError) as e:
            self.report_issue(
                station=station,
                issue_type=IssueType.INCOMPLETE_ANTENNA,
                severity=IssueSeverity.WARNING,
                description=f"Invalid antenna height value: {str(e)}",
                impact="Total antenna height calculation degraded",
                fallback_used="antenna_height = 0.0",
            )
            return 0.0

    def get_receiver_info_safe(
        self, device_session: Dict, station: str
    ) -> Dict[str, str]:
        """
        Safely extract receiver information with issue reporting
        """
        receiver_info = {
            "model": "UNKNOWN",
            "serial_number": "UNKNOWN",
            "firmware_version": "UNKNOWN",
        }

        try:
            receiver_data = device_session["gnss_receiver"]
            receiver_info["model"] = receiver_data.get("model", "UNKNOWN") or "UNKNOWN"
            receiver_info["serial_number"] = (
                receiver_data.get("serial_number", "UNKNOWN") or "UNKNOWN"
            )
            receiver_info["firmware_version"] = (
                receiver_data.get("firmware_version", "UNKNOWN") or "UNKNOWN"
            )

            # Report missing fields
            if receiver_info["serial_number"] == "UNKNOWN":
                self.report_issue(
                    station=station,
                    issue_type=IssueType.MISSING_SERIAL_NUMBERS,
                    severity=IssueSeverity.INFO,
                    description="Receiver serial number missing",
                    impact="GAMIT processing may be affected",
                    fallback_used="serial_number = UNKNOWN",
                )

            if receiver_info["firmware_version"] == "UNKNOWN":
                self.report_issue(
                    station=station,
                    issue_type=IssueType.MISSING_FIRMWARE,
                    severity=IssueSeverity.INFO,
                    description="Receiver firmware version missing",
                    impact="Version tracking unavailable",
                    fallback_used="firmware_version = UNKNOWN",
                )

        except KeyError:
            self.report_issue(
                station=station,
                issue_type=IssueType.MISSING_RECEIVER,
                severity=IssueSeverity.CRITICAL,
                description="Receiver data completely missing from device session",
                impact="GAMIT processing severely impacted",
                fallback_used="All receiver fields = UNKNOWN",
                context={"available_keys": list(device_session.keys())},
            )

        return receiver_info

    def save_issues_to_file(self, filepath: str) -> None:
        """Save collected issues to JSON file for analysis"""
        issues_data = {
            "report_generated": datetime.now().isoformat(),
            "total_issues": len(self.issues),
            "issues": [asdict(issue) for issue in self.issues],
        }

        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w") as f:
            json.dump(issues_data, f, indent=2)

        self.logger.info(f"Saved {len(self.issues)} data quality issues to {filepath}")

    def generate_summary_report(self) -> str:
        """Generate human-readable summary report for TOS team"""
        if not self.issues:
            return "No TOS data quality issues detected."

        # Group issues by type and severity
        by_type = {}
        by_severity = {}
        by_station = {}

        for issue in self.issues:
            # By type
            by_type[issue.issue_type] = by_type.get(issue.issue_type, 0) + 1
            # By severity
            by_severity[issue.severity] = by_severity.get(issue.severity, 0) + 1
            # By station
            by_station[issue.station] = by_station.get(issue.station, 0) + 1

        report = [
            f"TOS Data Quality Issues Report - {datetime.now().strftime('%Y-%m-%d')}",
            "=" * 60,
            "",
            f"Total Issues Found: {len(self.issues)}",
            f"Stations Affected: {len(by_station)}",
            "",
            "Issues by Severity:",
            "-" * 20,
        ]

        for severity in ["critical", "warning", "info"]:
            count = by_severity.get(severity, 0)
            if count > 0:
                report.append(f"  {severity.upper()}: {count}")

        report.extend(
            [
                "",
                "Issues by Type:",
                "-" * 15,
            ]
        )

        for issue_type, count in sorted(by_type.items()):
            report.append(f"  {issue_type}: {count}")

        report.extend(
            [
                "",
                "Affected Stations:",
                "-" * 18,
            ]
        )

        for station, count in sorted(by_station.items()):
            report.append(f"  {station}: {count} issues")

        report.extend(
            [
                "",
                "Detailed Issues:",
                "-" * 16,
            ]
        )

        # Group by station for detailed breakdown
        for station in sorted(by_station.keys()):
            station_issues = [
                issue for issue in self.issues if issue.station == station
            ]
            report.append(f"\n{station} ({len(station_issues)} issues):")

            for issue in station_issues:
                session_info = ""
                if issue.session_start:
                    session_info = f" [{issue.session_start}"
                    if issue.session_end:
                        session_info += f" to {issue.session_end}"
                    session_info += "]"

                report.append(
                    f"  • {issue.severity.upper()}: {issue.description}{session_info}"
                )
                if issue.impact:
                    report.append(f"    Impact: {issue.impact}")
                if issue.fallback_used:
                    report.append(f"    Fallback: {issue.fallback_used}")

        return "\n".join(report)

    def clear_issues(self):
        """Clear collected issues (for new processing session)"""
        self.issues.clear()
        self._session_cache.clear()


# Global instance for easy access across modules
data_quality_manager = DataQualityManager()
