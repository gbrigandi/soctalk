"""HIL Service that manages backend lifecycle and provides unified API."""

from __future__ import annotations

from typing import Any, Optional, Type

import structlog

from soctalk.hil.base import HILBackend, HILConnectionError
from soctalk.hil.models import HILRequest, HILResponse, EnrichmentSummary, MISPContextSummary
from soctalk.hil.inquiry import handle_inquiry
from soctalk.models.investigation import Investigation
from soctalk.models.verdict import Verdict
from soctalk.models.enums import VerdictDecision

logger = structlog.get_logger()


class HILService:
    """Service that manages HIL backends and provides a unified API.

    This service:
    - Manages the lifecycle of HIL backends
    - Converts Investigation/Verdict to HILRequest
    - Provides a simple API for the graph nodes
    """

    def __init__(self, backend: HILBackend):
        """Initialize HIL service with a backend.

        Args:
            backend: The HIL backend to use (Slack, Discord, CLI, etc.)
        """
        self._backend = backend

    @property
    def backend_name(self) -> str:
        """Get the name of the configured backend."""
        return self._backend.name

    @property
    def is_connected(self) -> bool:
        """Check if the backend is connected."""
        return self._backend.is_connected

    async def start(self) -> None:
        """Start the HIL service and connect the backend."""
        logger.info("hil_service_starting", backend=self._backend.name)
        await self._backend.start()

        # Set up inquiry handler for conversational HIL
        if hasattr(self._backend, 'set_inquiry_handler'):
            self._backend.set_inquiry_handler(self._handle_inquiry)
            logger.info("hil_service_inquiry_handler_configured")

        logger.info("hil_service_started", backend=self._backend.name)

    async def _handle_inquiry(
        self,
        investigation_id: str,
        inquiry: str,
        state: dict[str, Any],
        conversation_history: list[dict[str, str]],
    ) -> str:
        """Handle user inquiry by calling the inquiry handler.

        Args:
            investigation_id: The investigation ID.
            inquiry: The user's question.
            state: Current LangGraph state.
            conversation_history: Previous Q&A exchanges.

        Returns:
            The LLM's response to the inquiry.
        """
        logger.info(
            "hil_service_handling_inquiry",
            investigation_id=investigation_id,
            inquiry_preview=inquiry[:50],
        )

        response = await handle_inquiry(
            investigation_id=investigation_id,
            inquiry=inquiry,
            state=state,
            conversation_history=conversation_history,
        )

        logger.info(
            "hil_service_inquiry_handled",
            investigation_id=investigation_id,
            response_length=len(response),
        )

        return response

    async def stop(self) -> None:
        """Stop the HIL service and disconnect the backend."""
        logger.info("hil_service_stopping", backend=self._backend.name)
        await self._backend.stop()
        logger.info("hil_service_stopped", backend=self._backend.name)

    async def request_approval(
        self,
        investigation: Investigation,
        verdict: Optional[Verdict] = None,
        channel: Optional[str] = None,
        timeout: Optional[float] = None,
        state: Optional[dict[str, Any]] = None,
    ) -> HILResponse:
        """Request human approval for an investigation.

        Args:
            investigation: The investigation to review.
            verdict: Optional AI verdict to include.
            channel: Optional channel override.
            timeout: Optional timeout in seconds.
            state: Optional LangGraph state for conversational HIL inquiries.

        Returns:
            HILResponse with the human's decision.

        Raises:
            HILConnectionError: If not connected.
            HILTimeoutError: If no response received in time.
        """
        if not self._backend.is_connected:
            raise HILConnectionError(
                f"HIL backend '{self._backend.name}' is not connected"
            )

        # Convert investigation to HIL request
        request = self._build_request(investigation, verdict, channel, timeout)

        logger.info(
            "hil_service_requesting_approval",
            investigation_id=investigation.id,
            backend=self._backend.name,
        )

        response = await self._backend.request_approval(request, timeout, state)

        logger.info(
            "hil_service_approval_received",
            investigation_id=investigation.id,
            decision=response.decision.value,
            reviewer=response.reviewer,
        )

        return response

    def _build_request(
        self,
        investigation: Investigation,
        verdict: Optional[Verdict],
        channel: Optional[str],
        timeout: Optional[float],
    ) -> HILRequest:
        """Build HILRequest from Investigation and Verdict."""
        from soctalk.models.enums import Verdict as VerdictEnum

        # Count enrichment verdicts and build enrichment summaries
        malicious_count = 0
        suspicious_count = 0
        clean_count = 0
        enrichment_summaries = []

        for enrichment in investigation.enrichments:
            # Handle both EnrichmentResult objects and dicts
            if hasattr(enrichment, 'verdict'):
                # EnrichmentResult object
                v = enrichment.verdict.value.lower() if enrichment.verdict else ""
                if enrichment.verdict == VerdictEnum.MALICIOUS:
                    malicious_count += 1
                elif enrichment.verdict == VerdictEnum.SUSPICIOUS:
                    suspicious_count += 1
                elif enrichment.verdict == VerdictEnum.BENIGN:
                    clean_count += 1

                # Build EnrichmentSummary
                enrichment_summaries.append(EnrichmentSummary(
                    observable_value=enrichment.observable.value,
                    observable_type=enrichment.observable.type.value,
                    analyzer=enrichment.analyzer,
                    verdict=enrichment.verdict.value if enrichment.verdict else "unknown",
                    confidence=enrichment.confidence,
                    details=enrichment.details,
                ))
            else:
                # Dict fallback
                v = enrichment.get("verdict", "").lower()
                if v == "malicious":
                    malicious_count += 1
                elif v == "suspicious":
                    suspicious_count += 1
                elif v == "benign":
                    clean_count += 1

        # Extract findings as strings
        findings = []
        for finding in investigation.findings:
            if isinstance(finding, dict):
                desc = finding.get("description", "")
                sev = finding.get("severity", "")
                findings.append(f"[{sev.upper()}] {desc}" if sev else desc)
            elif hasattr(finding, 'description'):
                # Finding object
                sev = finding.severity.value if hasattr(finding, 'severity') and finding.severity else ""
                findings.append(f"[{sev.upper()}] {finding.description}" if sev else finding.description)
            else:
                findings.append(str(finding))

        # Extract MISP context if available
        misp_context_summary = None
        misp_context = investigation.misp_context

        if misp_context:
            # Extract unique event IDs from matches
            matched_events = []
            for match in misp_context.get("matches", []):
                matched_events.extend(match.get("event_ids", []))
            matched_events = list(set(matched_events))[:10]  # Dedupe and limit

            misp_context_summary = MISPContextSummary(
                iocs_checked=len(misp_context.get("checked_iocs", [])),
                iocs_matched=len(misp_context.get("matches", [])),
                threat_actors=misp_context.get("threat_actors", []),
                campaigns=misp_context.get("campaigns", []),
                warninglist_hits=len(misp_context.get("warninglist_hits", [])),
                matched_events=matched_events,
            )

        # Build request
        request = HILRequest(
            investigation_id=investigation.id,
            title=investigation.title,
            description=investigation.description or "",
            max_severity=investigation.max_severity,
            alert_count=len(investigation.alerts),
            created_at=investigation.created_at,
            malicious_count=malicious_count,
            suspicious_count=suspicious_count,
            clean_count=clean_count,
            findings=findings,
            enrichments=enrichment_summaries,
            misp_context=misp_context_summary,
            channel=channel,
            timeout_seconds=int(timeout) if timeout else 300,
        )

        # Add verdict info if available
        if verdict:
            request.ai_decision = verdict.decision
            request.ai_confidence = verdict.confidence
            request.ai_impact = verdict.potential_impact
            request.ai_urgency = verdict.urgency
            request.ai_assessment = verdict.threat_assessment
            request.ai_recommendation = verdict.recommendation
            request.ai_evidence = verdict.key_evidence or []

        return request


def create_hil_service(
    backend_type: str = "slack",
    **kwargs,
) -> HILService:
    """Factory function to create HIL service with specified backend.

    Args:
        backend_type: Backend type ('slack', 'discord', 'cli').
        **kwargs: Backend-specific configuration.
            For Slack:
                - bot_token: Slack Bot User OAuth Token
                - app_token: Slack App-Level Token
                - default_channel: Default channel ID
                - session_factory: Optional async session factory for race condition prevention

    Returns:
        Configured HILService.

    Raises:
        ValueError: If backend_type is not supported.
    """
    if backend_type == "slack":
        from soctalk.hil.backends.slack import SlackHILBackend

        required = ["bot_token", "app_token", "default_channel"]
        missing = [k for k in required if k not in kwargs]
        if missing:
            raise ValueError(f"Missing required Slack config: {missing}")

        backend = SlackHILBackend(
            bot_token=kwargs["bot_token"],
            app_token=kwargs["app_token"],
            default_channel=kwargs["default_channel"],
            session_factory=kwargs.get("session_factory"),  # Optional, for race condition prevention
        )

    elif backend_type == "discord":
        raise NotImplementedError("Discord backend not yet implemented")

    elif backend_type == "cli":
        raise NotImplementedError("CLI backend not yet implemented")

    else:
        raise ValueError(f"Unknown HIL backend type: {backend_type}")

    return HILService(backend)
