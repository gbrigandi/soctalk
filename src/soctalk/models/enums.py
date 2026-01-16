"""Enumeration types for SocTalk models."""

from enum import Enum


class Severity(str, Enum):
    """Alert/finding severity levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @classmethod
    def from_wazuh_level(cls, level: int) -> "Severity":
        """Convert Wazuh alert level (0-15) to Severity.

        Args:
            level: Wazuh alert level.

        Returns:
            Corresponding Severity enum value.
        """
        if level >= 12:
            return cls.CRITICAL
        elif level >= 8:
            return cls.HIGH
        elif level >= 4:
            return cls.MEDIUM
        else:
            return cls.LOW


class ObservableType(str, Enum):
    """Types of security observables/IOCs."""

    IP = "ip"
    DOMAIN = "domain"
    URL = "url"
    HASH_MD5 = "hash_md5"
    HASH_SHA1 = "hash_sha1"
    HASH_SHA256 = "hash_sha256"
    EMAIL = "email"
    FILENAME = "filename"
    FQDN = "fqdn"
    USER = "user"
    PROCESS = "process"
    REGISTRY_KEY = "registry_key"
    UNKNOWN = "unknown"


class Verdict(str, Enum):
    """Threat intelligence verdict for observables."""

    BENIGN = "benign"
    SUSPICIOUS = "suspicious"
    MALICIOUS = "malicious"
    UNKNOWN = "unknown"


class EvidenceStrength(str, Enum):
    """Strength of evidence supporting a finding."""

    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"
    CONCLUSIVE = "conclusive"


class ImpactLevel(str, Enum):
    """Potential impact level of an incident."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Urgency(str, Enum):
    """Urgency level for response."""

    ROUTINE = "routine"
    ELEVATED = "elevated"
    URGENT = "urgent"
    IMMEDIATE = "immediate"


class InvestigationStatus(str, Enum):
    """Status of an investigation."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    AWAITING_ENRICHMENT = "awaiting_enrichment"
    AWAITING_VERDICT = "awaiting_verdict"
    AWAITING_HUMAN = "awaiting_human"
    ESCALATED = "escalated"
    CLOSED = "closed"


class Phase(str, Enum):
    """Investigation phase."""

    TRIAGE = "triage"
    ENRICHMENT = "enrichment"
    ANALYSIS = "analysis"
    VERDICT = "verdict"
    ESCALATION = "escalation"
    CLOSED = "closed"


class VerdictDecision(str, Enum):
    """Decision from the verdict stage."""

    ESCALATE = "escalate"
    CLOSE = "close"
    NEEDS_MORE_INFO = "needs_more_info"


class HumanDecision(str, Enum):
    """Decision from human review."""

    APPROVE = "approve"
    REJECT = "reject"
    MORE_INFO = "more_info"


class AssetCriticality(str, Enum):
    """Criticality level of an asset."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
