"""Alert correlator for grouping related alerts."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

import structlog

from soctalk.config import get_config
from soctalk.models.alerts import Alert
from soctalk.models.investigation import Investigation

logger = structlog.get_logger()


class AlertCorrelator:
    """Correlates related alerts into investigation batches.

    Correlation criteria:
    - Same source IP
    - Same destination/affected agent
    - Same file hash
    - Time window proximity
    - Same rule group/category
    """

    def __init__(self, window_minutes: Optional[int] = None):
        """Initialize the correlator.

        Args:
            window_minutes: Time window for correlation. Defaults to config value.
        """
        config = get_config()
        self.window_minutes = window_minutes or config.polling.correlation_window_minutes

    def correlate(self, alerts: list[Alert]) -> list[Investigation]:
        """Group related alerts into investigations.

        Args:
            alerts: List of alerts to correlate.

        Returns:
            List of investigations, each containing correlated alerts.
        """
        if not alerts:
            return []

        logger.debug("correlating_alerts", count=len(alerts))

        # Group alerts by correlation keys
        groups: dict[str, list[Alert]] = defaultdict(list)

        for alert in alerts:
            keys = self._get_correlation_keys(alert)
            if keys:
                # Use the first (strongest) correlation key
                groups[keys[0]].append(alert)
            else:
                # No correlation key - standalone investigation
                groups[f"standalone_{alert.id}"].append(alert)

        # Merge groups that share alerts or have overlapping time windows
        merged_groups = self._merge_overlapping_groups(groups)

        # Create investigations from groups
        investigations = []
        for group_alerts in merged_groups:
            investigation = self._create_investigation(group_alerts)
            investigations.append(investigation)

        # Sort by max severity (critical first)
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        investigations.sort(
            key=lambda inv: severity_order.get(inv.max_severity.value, 4)
        )

        logger.info(
            "alerts_correlated",
            input_alerts=len(alerts),
            investigations=len(investigations),
        )

        return investigations

    def _get_correlation_keys(self, alert: Alert) -> list[str]:
        """Generate correlation keys for an alert.

        Args:
            alert: Alert to generate keys for.

        Returns:
            List of correlation keys, strongest first.
        """
        keys = []

        # Correlate by agent
        if alert.source.agent_name and alert.source.agent_name != "unknown":
            keys.append(f"agent:{alert.source.agent_name}")

        # Correlate by observables
        for obs in alert.observables:
            if obs.type.value == "ip":
                keys.append(f"ip:{obs.value}")
            elif obs.type.value in ("hash_md5", "hash_sha256", "hash_sha1"):
                keys.append(f"hash:{obs.value}")
            elif obs.type.value == "domain":
                keys.append(f"domain:{obs.value}")

        # Correlate by rule group (extract from description if available)
        rule_groups = self._extract_rule_groups(alert)
        for group in rule_groups:
            keys.append(f"rulegroup:{group}")

        return keys

    def _extract_rule_groups(self, alert: Alert) -> list[str]:
        """Extract rule groups from alert.

        Args:
            alert: Alert to extract groups from.

        Returns:
            List of rule group names.
        """
        groups = []

        # Common Wazuh rule group patterns
        desc_lower = alert.rule_description.lower()

        group_patterns = [
            ("sysmon", "sysmon"),
            ("authentication", "auth"),
            ("brute", "bruteforce"),
            ("malware", "malware"),
            ("rootkit", "rootkit"),
            ("web", "web_attack"),
            ("sql", "sql_injection"),
            ("file integrity", "fim"),
            ("vulnerability", "vuln"),
        ]

        for pattern, group in group_patterns:
            if pattern in desc_lower:
                groups.append(group)

        return groups

    def _merge_overlapping_groups(
        self, groups: dict[str, list[Alert]]
    ) -> list[list[Alert]]:
        """Merge groups that have overlapping alerts or time windows.

        Args:
            groups: Initial alert groups.

        Returns:
            List of merged alert lists.
        """
        # For now, simple approach: don't merge, just deduplicate
        # Future: implement union-find for proper merging

        merged = []
        seen_alert_ids = set()

        for group_alerts in groups.values():
            unique_alerts = []
            for alert in group_alerts:
                if alert.id not in seen_alert_ids:
                    unique_alerts.append(alert)
                    seen_alert_ids.add(alert.id)

            if unique_alerts:
                # Check time window
                unique_alerts = self._filter_by_time_window(unique_alerts)
                if unique_alerts:
                    merged.append(unique_alerts)

        return merged

    def _filter_by_time_window(self, alerts: list[Alert]) -> list[Alert]:
        """Filter alerts to those within the time window.

        Args:
            alerts: Alerts to filter.

        Returns:
            Alerts within the correlation window.
        """
        if not alerts:
            return []

        # Find the most recent alert
        most_recent = max(alerts, key=lambda a: a.timestamp)
        cutoff = most_recent.timestamp - timedelta(minutes=self.window_minutes)

        # Filter to alerts within window
        return [a for a in alerts if a.timestamp >= cutoff]

    def _create_investigation(self, alerts: list[Alert]) -> Investigation:
        """Create an investigation from correlated alerts.

        Args:
            alerts: Correlated alerts.

        Returns:
            New Investigation object.
        """
        investigation = Investigation()

        for alert in alerts:
            investigation.add_alert(alert)

        # Generate title
        investigation.title = investigation.generate_title()

        logger.debug(
            "investigation_created",
            id=investigation.id,
            alert_count=len(alerts),
            title=investigation.title[:50],
        )

        return investigation


def correlate_and_prioritize(alerts: list[Alert]) -> list[Investigation]:
    """Convenience function to correlate and prioritize alerts.

    Args:
        alerts: List of alerts to process.

    Returns:
        List of investigations sorted by priority.
    """
    correlator = AlertCorrelator()
    return correlator.correlate(alerts)
