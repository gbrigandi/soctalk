"""Cortex worker node for threat intelligence enrichment."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

import structlog
from langgraph.config import get_config as get_langgraph_config

from soctalk.mcp.bindings import get_cortex_client
from soctalk.models.enums import ObservableType, Verdict, Phase
from soctalk.models.observables import Observable, EnrichmentResult
from soctalk.persistence.emitter import get_emitter_from_config, get_investigation_id_from_state

logger = structlog.get_logger()

# Mapping of observable types to Cortex analyzers
ANALYZER_MAP = {
    ObservableType.IP: [
        ("analyze_ip_with_abuseipdb", "AbuseIPDB"),
    ],
    ObservableType.URL: [
        ("scan_url_with_virustotal", "VirusTotal"),
        ("analyze_url_with_urlscan_io", "Urlscan.io"),
    ],
    ObservableType.HASH_MD5: [
        ("scan_hash_with_virustotal", "VirusTotal"),
    ],
    ObservableType.HASH_SHA1: [
        ("scan_hash_with_virustotal", "VirusTotal"),
    ],
    ObservableType.HASH_SHA256: [
        ("scan_hash_with_virustotal", "VirusTotal"),
    ],
    ObservableType.DOMAIN: [
        ("analyze_with_abusefinder", "AbuseFinder"),
    ],
    ObservableType.EMAIL: [
        ("analyze_with_abusefinder", "AbuseFinder"),
    ],
    ObservableType.FQDN: [
        ("analyze_with_abusefinder", "AbuseFinder"),
    ],
}


async def cortex_worker_node(
    state: dict[str, Any],
) -> dict[str, Any]:
    """Cortex worker node - handles threat intelligence enrichment.

    This worker enriches observables using Cortex analyzers:
    - IP reputation (AbuseIPDB)
    - URL scanning (VirusTotal, Urlscan.io)
    - Hash analysis (VirusTotal)
    - Domain/email analysis (AbuseFinder)

    Args:
        state: Current graph state.

    Returns:
        Updated state dictionary.
    """
    try:
        config = get_langgraph_config()
    except RuntimeError:
        config = None

    logger.info("cortex_worker_started")

    client = get_cortex_client()
    investigation = state.get("investigation", {})
    pending_observables = state.get("pending_observables", [])

    # Get emitter for event emission
    emitter = get_emitter_from_config(config)
    investigation_id = get_investigation_id_from_state(state)

    # Convert dict observables back to Observable objects if needed
    observables_to_process = []
    for obs in pending_observables[:10]:  # Process up to 10 at a time
        if isinstance(obs, dict):
            observables_to_process.append(Observable(**obs))
        else:
            observables_to_process.append(obs)

    if not observables_to_process:
        logger.info("no_observables_to_enrich")
        state["current_phase"] = Phase.ANALYSIS.value
        return state

    enrichments = investigation.get("enrichments", [])
    processed_values = set()

    for observable in observables_to_process:
        if observable.value in processed_values:
            continue

        # Determine analyzer for this observable
        analyzers = ANALYZER_MAP.get(observable.type, [])
        analyzer_name = analyzers[0][1] if analyzers else "unknown"

        logger.info(
            "enriching_observable",
            type=observable.type.value,
            value=observable.value[:50],
        )

        # Emit enrichment requested event
        if emitter and investigation_id:
            try:
                await emitter.emit_enrichment_requested(
                    investigation_id=investigation_id,
                    observable_type=observable.type.value,
                    observable_value=observable.value[:200],
                    analyzer=analyzer_name,
                    idempotency_key=f"enrich-{investigation_id}-{observable.type.value}-{observable.value[:50]}",
                )
            except Exception as emit_error:
                logger.warning("event_emission_failed", error=str(emit_error))

        start_time = time.time()
        try:
            enrichment = await _enrich_observable(client, observable)
            elapsed_ms = int((time.time() - start_time) * 1000)

            if enrichment:
                enrichments.append(enrichment.model_dump())
                processed_values.add(observable.value)

                # Emit enrichment completed event
                if emitter and investigation_id:
                    try:
                        await emitter.emit_enrichment_completed(
                            investigation_id=investigation_id,
                            observable_type=observable.type.value,
                            observable_value=observable.value[:200],
                            analyzer=enrichment.analyzer,
                            verdict=enrichment.verdict.value,
                            score=enrichment.confidence,
                            response_time_ms=elapsed_ms,
                        )
                    except Exception as emit_error:
                        logger.warning("event_emission_failed", error=str(emit_error))

        except Exception as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            logger.warning(
                "enrichment_failed",
                observable=observable.value[:50],
                error=str(e),
            )

            # Emit enrichment failed event
            if emitter and investigation_id:
                try:
                    await emitter.emit_enrichment_failed(
                        investigation_id=investigation_id,
                        observable_type=observable.type.value,
                        observable_value=observable.value[:200],
                        analyzer=analyzer_name,
                        error=str(e)[:500],
                    )
                except Exception as emit_error:
                    logger.warning("event_emission_failed", error=str(emit_error))

            # Add a failed enrichment result
            failed_enrichment = EnrichmentResult(
                observable=observable,
                analyzer="unknown",
                verdict=Verdict.UNKNOWN,
                confidence=0.0,
                error=str(e),
            )
            enrichments.append(failed_enrichment.model_dump())
            processed_values.add(observable.value)

    # Update state
    investigation["enrichments"] = enrichments

    # Remove processed observables from pending
    new_pending = [
        o for o in pending_observables
        if (o.get("value") if isinstance(o, dict) else o.value) not in processed_values
    ]

    state["investigation"] = investigation
    state["pending_observables"] = new_pending
    state["current_enrichment_batch"] = []
    state["last_updated"] = datetime.now().isoformat()

    # If no more pending, move to analysis phase
    if not new_pending:
        state["current_phase"] = Phase.ANALYSIS.value

    logger.info(
        "cortex_worker_completed",
        enriched=len(processed_values),
        remaining=len(new_pending),
    )

    return state


async def _enrich_observable(client: Any, observable: Observable) -> EnrichmentResult | None:
    """Enrich a single observable using appropriate Cortex analyzer.

    Args:
        client: Cortex MCP client.
        observable: Observable to enrich.

    Returns:
        EnrichmentResult or None if enrichment fails.
    """
    analyzers = ANALYZER_MAP.get(observable.type, [])

    if not analyzers:
        logger.debug("no_analyzer_for_type", type=observable.type.value)
        return EnrichmentResult(
            observable=observable,
            analyzer="none",
            verdict=Verdict.UNKNOWN,
            confidence=0.0,
            details={"note": f"No analyzer available for type {observable.type.value}"},
        )

    # Try the first available analyzer for this type
    tool_name, analyzer_name = analyzers[0]

    # Build arguments based on tool
    # Use 15 retries (~60 seconds) to give analyzers time to complete
    if tool_name == "analyze_ip_with_abuseipdb":
        args = {"ip": observable.value, "max_retries": 15}
    elif tool_name in ("scan_url_with_virustotal", "analyze_url_with_urlscan_io"):
        args = {"url": observable.value, "max_retries": 15}
    elif tool_name == "scan_hash_with_virustotal":
        args = {"hash": observable.value, "max_retries": 15}
    elif tool_name == "analyze_with_abusefinder":
        # Map observable type to AbuseFinder data_type
        data_type_map = {
            ObservableType.DOMAIN: "domain",
            ObservableType.EMAIL: "mail",
            ObservableType.FQDN: "fqdn",
            ObservableType.IP: "ip",
            ObservableType.URL: "url",
        }
        args = {
            "data": observable.value,
            "data_type": data_type_map.get(observable.type, "domain"),
            "max_retries": 15,
        }
    else:
        args = {"data": observable.value, "max_retries": 15}

    try:
        result = await client.call_tool(tool_name, args)

        # Parse the result
        verdict, confidence, details = _parse_enrichment_result(result, tool_name)

        return EnrichmentResult(
            observable=observable,
            analyzer=analyzer_name,
            verdict=verdict,
            confidence=confidence,
            details=details,
        )

    except Exception as e:
        logger.error(
            "analyzer_failed",
            tool=tool_name,
            observable=observable.value[:50],
            error=str(e),
        )
        raise


def _parse_enrichment_result(
    result: str, tool_name: str
) -> tuple[Verdict, float, dict[str, Any]]:
    """Parse enrichment result and determine verdict.

    Args:
        result: Raw result from Cortex tool.
        tool_name: Name of the tool used.

    Returns:
        Tuple of (verdict, confidence, details).
    """
    details: dict[str, Any] = {"raw_result": result[:1000] if result else ""}

    # Try to parse as JSON
    try:
        if result and result.strip().startswith("{"):
            parsed = json.loads(result)
            details = parsed
    except json.JSONDecodeError:
        pass

    # Determine verdict based on tool and result
    verdict = Verdict.UNKNOWN
    confidence = 0.5

    result_lower = result.lower() if result else ""

    # AbuseIPDB patterns
    if tool_name == "analyze_ip_with_abuseipdb":
        if "abuse confidence score" in result_lower:
            # Extract score
            import re
            score_match = re.search(r"abuse confidence score[:\s]*(\d+)", result_lower)
            if score_match:
                score = int(score_match.group(1))
                if score >= 80:
                    verdict = Verdict.MALICIOUS
                    confidence = score / 100
                elif score >= 30:
                    verdict = Verdict.SUSPICIOUS
                    confidence = score / 100
                else:
                    verdict = Verdict.BENIGN
                    confidence = 1 - (score / 100)

    # VirusTotal patterns
    elif "virustotal" in tool_name.lower():
        if "malicious" in result_lower:
            # Look for detection ratio
            import re
            ratio_match = re.search(r"(\d+)/(\d+)", result_lower)
            if ratio_match:
                detections = int(ratio_match.group(1))
                total = int(ratio_match.group(2))
                if total > 0:
                    ratio = detections / total
                    if ratio >= 0.3:
                        verdict = Verdict.MALICIOUS
                        confidence = min(0.95, 0.5 + ratio)
                    elif ratio >= 0.1:
                        verdict = Verdict.SUSPICIOUS
                        confidence = 0.5 + ratio
                    else:
                        verdict = Verdict.BENIGN
                        confidence = 1 - ratio
            else:
                # Just the word "malicious" without ratio
                verdict = Verdict.SUSPICIOUS
                confidence = 0.6

        elif "clean" in result_lower or "harmless" in result_lower:
            verdict = Verdict.BENIGN
            confidence = 0.8

    # Urlscan.io patterns
    elif "urlscan" in tool_name.lower():
        if "malicious" in result_lower or "phishing" in result_lower:
            verdict = Verdict.MALICIOUS
            confidence = 0.8
        elif "suspicious" in result_lower:
            verdict = Verdict.SUSPICIOUS
            confidence = 0.6
        elif "safe" in result_lower or "benign" in result_lower:
            verdict = Verdict.BENIGN
            confidence = 0.7

    # AbuseFinder patterns
    elif "abusefinder" in tool_name.lower():
        if "abuse" in result_lower and "found" in result_lower:
            verdict = Verdict.SUSPICIOUS
            confidence = 0.6
        elif "no abuse" in result_lower:
            verdict = Verdict.BENIGN
            confidence = 0.7

    # Generic fallback patterns
    if verdict == Verdict.UNKNOWN:
        if any(word in result_lower for word in ["malware", "threat", "attack", "dangerous"]):
            verdict = Verdict.MALICIOUS
            confidence = 0.7
        elif any(word in result_lower for word in ["suspicious", "potentially", "risky"]):
            verdict = Verdict.SUSPICIOUS
            confidence = 0.5
        elif any(word in result_lower for word in ["clean", "safe", "benign", "legitimate"]):
            verdict = Verdict.BENIGN
            confidence = 0.6

    return verdict, confidence, details
