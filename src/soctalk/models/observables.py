"""Observable and enrichment models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from soctalk.models.enums import ObservableType, Verdict


class Observable(BaseModel):
    """A security observable/IOC extracted from alerts or investigation."""

    value: str = Field(..., description="The observable value (IP, hash, domain, etc.)")
    type: ObservableType = Field(..., description="Type of observable")
    source: str = Field(..., description="Where this observable was extracted from")
    first_seen: datetime = Field(default_factory=datetime.now, description="When first observed")
    context: Optional[str] = Field(None, description="Additional context about the observable")
    tags: list[str] = Field(default_factory=list, description="Tags/labels for the observable")

    def __hash__(self) -> int:
        """Make observable hashable for deduplication."""
        return hash((self.value, self.type))

    def __eq__(self, other: object) -> bool:
        """Check equality based on value and type."""
        if not isinstance(other, Observable):
            return False
        return self.value == other.value and self.type == other.type

    @classmethod
    def detect_type(cls, value: str) -> ObservableType:
        """Attempt to auto-detect observable type from value.

        Args:
            value: The observable value.

        Returns:
            Detected ObservableType.
        """
        import re

        value = value.strip()

        # IP address patterns
        ipv4_pattern = r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$"
        if re.match(ipv4_pattern, value):
            return ObservableType.IP

        # Hash patterns
        if re.match(r"^[a-fA-F0-9]{32}$", value):
            return ObservableType.HASH_MD5
        if re.match(r"^[a-fA-F0-9]{40}$", value):
            return ObservableType.HASH_SHA1
        if re.match(r"^[a-fA-F0-9]{64}$", value):
            return ObservableType.HASH_SHA256

        # URL pattern
        if value.startswith(("http://", "https://")):
            return ObservableType.URL

        # Email pattern
        if re.match(r"^[^@]+@[^@]+\.[^@]+$", value):
            return ObservableType.EMAIL

        # Domain/FQDN pattern
        if re.match(r"^[a-zA-Z0-9][a-zA-Z0-9-]*(\.[a-zA-Z0-9][a-zA-Z0-9-]*)+$", value):
            return ObservableType.DOMAIN

        return ObservableType.UNKNOWN


class EnrichmentResult(BaseModel):
    """Result of enriching an observable with threat intelligence."""

    observable: Observable = Field(..., description="The enriched observable")
    analyzer: str = Field(..., description="Name of the analyzer used")
    verdict: Verdict = Field(default=Verdict.UNKNOWN, description="Threat verdict")
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Confidence in the verdict (0-1)"
    )
    details: dict[str, Any] = Field(default_factory=dict, description="Raw analyzer results")
    enrichment_time: datetime = Field(
        default_factory=datetime.now, description="When enrichment was performed"
    )
    error: Optional[str] = Field(None, description="Error message if enrichment failed")

    @property
    def is_malicious(self) -> bool:
        """Check if verdict indicates malicious activity."""
        return self.verdict == Verdict.MALICIOUS

    @property
    def is_suspicious(self) -> bool:
        """Check if verdict indicates suspicious activity."""
        return self.verdict in (Verdict.SUSPICIOUS, Verdict.MALICIOUS)

    def to_summary(self) -> str:
        """Generate a human-readable summary of the enrichment.

        Returns:
            Summary string.
        """
        verdict_emoji = {
            Verdict.BENIGN: "✅",
            Verdict.SUSPICIOUS: "⚠️",
            Verdict.MALICIOUS: "🔴",
            Verdict.UNKNOWN: "❓",
        }
        emoji = verdict_emoji.get(self.verdict, "❓")

        return (
            f"{emoji} {self.observable.type.value.upper()}: {self.observable.value}\n"
            f"   Analyzer: {self.analyzer}\n"
            f"   Verdict: {self.verdict.value} (confidence: {self.confidence:.0%})"
        )
