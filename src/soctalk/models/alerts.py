"""Alert models for security events from Wazuh."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from soctalk.models.enums import Severity, ObservableType
from soctalk.models.observables import Observable


class AlertSource(BaseModel):
    """Source information for an alert."""

    agent_id: str = Field(..., description="Wazuh agent ID")
    agent_name: str = Field(..., description="Wazuh agent name")
    agent_ip: Optional[str] = Field(None, description="Agent IP address")


class Alert(BaseModel):
    """A security alert from Wazuh SIEM."""

    id: str = Field(..., description="Unique alert ID")
    timestamp: datetime = Field(..., description="Alert timestamp")
    severity: Severity = Field(..., description="Alert severity level")
    level: int = Field(..., ge=0, le=15, description="Wazuh alert level (0-15)")
    rule_id: Optional[str] = Field(None, description="Rule ID that triggered the alert")
    rule_description: str = Field(..., description="Description of the rule/alert")
    source: AlertSource = Field(..., description="Source agent information")
    raw_data: dict[str, Any] = Field(default_factory=dict, description="Raw alert data")
    observables: list[Observable] = Field(
        default_factory=list, description="Extracted observables"
    )
    processed: bool = Field(default=False, description="Whether alert has been processed")

    @classmethod
    def from_wazuh_response(cls, alert_text: str, raw: Optional[dict] = None) -> Optional["Alert"]:
        """Parse an alert from Wazuh MCP tool response.

        Args:
            alert_text: Text response from get_wazuh_alert_summary.
            raw: Optional raw alert data.

        Returns:
            Parsed Alert or None if parsing fails.
        """
        # Parse the formatted alert text
        # Format: Alert ID: xxx\nTime: xxx\nAgent: xxx\nLevel: xxx\nDescription: xxx

        lines = alert_text.strip().split("\n")
        data: dict[str, str] = {}

        for line in lines:
            if ":" in line:
                key, value = line.split(":", 1)
                data[key.strip()] = value.strip()

        try:
            alert_id = data.get("Alert ID", "")
            timestamp_str = data.get("Time", "")
            agent_name = data.get("Agent", "unknown")
            level = int(data.get("Level", "0"))
            description = data.get("Description", "")

            # Parse timestamp
            try:
                timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                timestamp = datetime.now()

            # Create source
            source = AlertSource(
                agent_id="unknown",  # Not in summary response
                agent_name=agent_name,
            )

            # Create alert
            alert = cls(
                id=alert_id,
                timestamp=timestamp,
                severity=Severity.from_wazuh_level(level),
                level=level,
                rule_description=description,
                source=source,
                raw_data=raw or {"text": alert_text},
            )

            # Extract observables from description and raw data
            alert.observables = alert._extract_observables()

            return alert

        except Exception:
            return None

    def _extract_observables(self) -> list[Observable]:
        """Extract observables from alert data.

        Returns:
            List of extracted observables.
        """
        observables: list[Observable] = []
        seen: set[tuple[str, ObservableType]] = set()

        # Include raw_data text and any explicit Source IP/Destination IP fields
        text_to_search = f"{self.rule_description} {str(self.raw_data)}"

        # Also extract from structured fields if they exist in raw_data
        if isinstance(self.raw_data, dict):
            text_content = self.raw_data.get("text", "")
            if text_content:
                text_to_search += f" {text_content}"

        # Extract IP addresses (include all IPs for investigation)
        ip_pattern = r"\b(?:\d{1,3}\.){3}\d{1,3}\b"
        for match in re.findall(ip_pattern, text_to_search):
            key = (match, ObservableType.IP)
            if key not in seen:
                seen.add(key)
                is_private = self._is_private_ip(match)
                observables.append(
                    Observable(
                        value=match,
                        type=ObservableType.IP,
                        source=f"alert:{self.id}",
                        tags=["private_ip", "internal"] if is_private else [],
                    )
                )

        # Extract MD5 hashes
        md5_pattern = r"\b[a-fA-F0-9]{32}\b"
        for match in re.findall(md5_pattern, text_to_search):
            key = (match.lower(), ObservableType.HASH_MD5)
            if key not in seen:
                seen.add(key)
                observables.append(
                    Observable(
                        value=match.lower(),
                        type=ObservableType.HASH_MD5,
                        source=f"alert:{self.id}",
                    )
                )

        # Extract SHA256 hashes
        sha256_pattern = r"\b[a-fA-F0-9]{64}\b"
        for match in re.findall(sha256_pattern, text_to_search):
            key = (match.lower(), ObservableType.HASH_SHA256)
            if key not in seen:
                seen.add(key)
                observables.append(
                    Observable(
                        value=match.lower(),
                        type=ObservableType.HASH_SHA256,
                        source=f"alert:{self.id}",
                    )
                )

        # Extract URLs
        url_pattern = r"https?://[^\s<>\"'{}|\\^`\[\]]+"
        for match in re.findall(url_pattern, text_to_search):
            key = (match, ObservableType.URL)
            if key not in seen:
                seen.add(key)
                observables.append(
                    Observable(
                        value=match,
                        type=ObservableType.URL,
                        source=f"alert:{self.id}",
                    )
                )

        # Extract domains (simple pattern)
        domain_pattern = r"\b(?:[a-zA-Z0-9-]+\.)+(?:com|net|org|io|edu|gov|mil|co|uk|de|ru|cn)\b"
        for match in re.findall(domain_pattern, text_to_search):
            key = (match.lower(), ObservableType.DOMAIN)
            if key not in seen:
                seen.add(key)
                observables.append(
                    Observable(
                        value=match.lower(),
                        type=ObservableType.DOMAIN,
                        source=f"alert:{self.id}",
                    )
                )

        return observables

    @staticmethod
    def _is_private_ip(ip: str) -> bool:
        """Check if an IP address is private/local.

        Args:
            ip: IP address string.

        Returns:
            True if private/local, False otherwise.
        """
        parts = ip.split(".")
        if len(parts) != 4:
            return True

        try:
            octets = [int(p) for p in parts]
        except ValueError:
            return True

        # Check private ranges
        if octets[0] == 10:
            return True
        if octets[0] == 172 and 16 <= octets[1] <= 31:
            return True
        if octets[0] == 192 and octets[1] == 168:
            return True
        if octets[0] == 127:
            return True
        if octets[0] == 0:
            return True

        return False

    def to_summary(self) -> str:
        """Generate a human-readable summary of the alert.

        Returns:
            Summary string.
        """
        severity_emoji = {
            Severity.LOW: "🟢",
            Severity.MEDIUM: "🟡",
            Severity.HIGH: "🟠",
            Severity.CRITICAL: "🔴",
        }
        emoji = severity_emoji.get(self.severity, "⚪")

        observables_str = ""
        if self.observables:
            obs_list = [f"{o.type.value}: {o.value}" for o in self.observables[:5]]
            observables_str = f"\n   Observables: {', '.join(obs_list)}"
            if len(self.observables) > 5:
                observables_str += f" (+{len(self.observables) - 5} more)"

        return (
            f"{emoji} [{self.severity.value.upper()}] {self.rule_description}\n"
            f"   Alert ID: {self.id}\n"
            f"   Time: {self.timestamp.isoformat()}\n"
            f"   Source: {self.source.agent_name}"
            f"{observables_str}"
        )
