"""MISP worker node for threat intelligence contextualization."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import structlog

from soctalk.mcp.bindings import get_misp_client
from soctalk.models.enums import ObservableType, Verdict, Phase, Severity
from soctalk.models.observables import Observable

logger = structlog.get_logger()

# Mapping of observable types to MISP search functions
MISP_SEARCHABLE_TYPES = {
    ObservableType.IP,
    ObservableType.DOMAIN,
    ObservableType.URL,
    ObservableType.HASH_MD5,
    ObservableType.HASH_SHA1,
    ObservableType.HASH_SHA256,
    ObservableType.EMAIL,
    ObservableType.FQDN,
}


async def misp_worker_node(state: dict[str, Any]) -> dict[str, Any]:
    """MISP worker node - handles threat intelligence contextualization.

    This worker enriches observables using MISP threat intelligence:
    - IOC lookup for campaign/threat actor attribution
    - Event context (related indicators, galaxies)
    - Warninglist checks for false positive reduction
    - Sighting history for prevalence assessment

    Args:
        state: Current graph state.

    Returns:
        Updated state dictionary.
    """
    logger.info("misp_worker_started")

    client = get_misp_client()
    investigation = state.get("investigation", {})

    # Get observables that haven't been checked with MISP yet
    observables = investigation.get("observables", [])
    misp_context = investigation.get("misp_context") or {}
    checked_values = set(misp_context.get("checked_iocs", []))

    # Convert dict observables back to Observable objects if needed
    observables_to_check = []
    for obs in observables:
        if isinstance(obs, dict):
            obs_obj = Observable(**obs)
        else:
            obs_obj = obs

        # Only check searchable types that haven't been checked
        if obs_obj.type in MISP_SEARCHABLE_TYPES and obs_obj.value not in checked_values:
            observables_to_check.append(obs_obj)

    if not observables_to_check:
        logger.info("no_observables_to_check_in_misp")
        return state

    # Process observables (limit to 10 per iteration)
    misp_matches = misp_context.get("matches", [])
    misp_events = misp_context.get("events", {})
    misp_threat_actors = misp_context.get("threat_actors", [])
    misp_campaigns = misp_context.get("campaigns", [])
    warninglist_hits = misp_context.get("warninglist_hits", [])
    findings = investigation.get("findings", [])

    for observable in observables_to_check[:10]:
        logger.info(
            "checking_misp_for_ioc",
            type=observable.type.value,
            value=observable.value[:50],
        )

        try:
            # Search for the IOC in MISP
            ioc_result = await _search_ioc(client, observable)
            checked_values.add(observable.value)

            if ioc_result:
                misp_matches.append(ioc_result)

                # Get event context for matched IOCs
                for event_id in ioc_result.get("event_ids", [])[:3]:  # Limit to 3 events
                    if event_id not in misp_events:
                        event_context = await _get_event_context(client, event_id)
                        if event_context:
                            misp_events[event_id] = event_context

                            # Extract threat actors and campaigns
                            for ta in event_context.get("threat_actors", []):
                                if ta not in misp_threat_actors:
                                    misp_threat_actors.append(ta)
                            for campaign in event_context.get("campaigns", []):
                                if campaign not in misp_campaigns:
                                    misp_campaigns.append(campaign)

            # Check warninglists for false positive detection
            warninglist_result = await _check_warninglist(client, observable)
            if warninglist_result and warninglist_result.get("on_warninglist"):
                warninglist_hits.append(warninglist_result)

        except Exception as e:
            logger.warning(
                "misp_check_failed",
                observable=observable.value[:50],
                error=str(e),
            )

    # Generate findings based on MISP context
    new_findings = _generate_misp_findings(
        misp_matches, misp_threat_actors, misp_campaigns, warninglist_hits
    )
    findings.extend(new_findings)

    # Update MISP context
    misp_context = {
        "checked_iocs": list(checked_values),
        "matches": misp_matches,
        "events": misp_events,
        "threat_actors": misp_threat_actors,
        "campaigns": misp_campaigns,
        "warninglist_hits": warninglist_hits,
        "last_checked": datetime.now().isoformat(),
    }

    # Update investigation
    investigation["misp_context"] = misp_context
    investigation["findings"] = findings

    state["investigation"] = investigation
    state["last_updated"] = datetime.now().isoformat()

    logger.info(
        "misp_worker_completed",
        checked=len(checked_values),
        matches=len(misp_matches),
        threat_actors=len(misp_threat_actors),
        warninglist_hits=len(warninglist_hits),
    )

    return state


async def _search_ioc(client: Any, observable: Observable) -> dict[str, Any] | None:
    """Search for an IOC in MISP.

    Args:
        client: MISP MCP client.
        observable: Observable to search.

    Returns:
        Match result or None if not found.
    """
    try:
        result = await client.call_tool(
            "search_misp_ioc",
            {"value": observable.value}
        )

        if not result or "No " in result and " found" in result:
            return None

        # Parse the result
        match_info = {
            "value": observable.value,
            "type": observable.type.value,
            "event_ids": [],
            "categories": [],
            "tags": [],
            "to_ids": False,
            "raw_result": result[:500],
        }

        # Extract event IDs
        event_matches = re.findall(r"Event ID:\s*(\d+)", result)
        match_info["event_ids"] = list(set(event_matches))

        # Extract categories
        category_matches = re.findall(r"Category:\s*([^\n]+)", result)
        match_info["categories"] = list(set(category_matches))

        # Extract tags
        tags_match = re.search(r"Tags:\s*([^\n]+)", result)
        if tags_match:
            match_info["tags"] = [t.strip() for t in tags_match.group(1).split(",")]

        # Check for to_ids
        if "To IDS: true" in result or "to_ids: true" in result.lower():
            match_info["to_ids"] = True

        return match_info

    except Exception as e:
        logger.error("misp_search_failed", error=str(e))
        raise


async def _get_event_context(client: Any, event_id: str) -> dict[str, Any] | None:
    """Get full context for a MISP event.

    Args:
        client: MISP MCP client.
        event_id: Event ID to look up.

    Returns:
        Event context or None if not found.
    """
    try:
        result = await client.call_tool(
            "get_misp_event_context",
            {"event_id": event_id}
        )

        if not result:
            return None

        context = {
            "event_id": event_id,
            "info": "",
            "threat_level": "",
            "threat_actors": [],
            "campaigns": [],
            "mitre_techniques": [],
            "tags": [],
            "attribute_count": 0,
            "raw_result": result[:1000],
        }

        # Extract event info
        info_match = re.search(r"Info:\s*([^\n]+)", result)
        if info_match:
            context["info"] = info_match.group(1).strip()

        # Extract threat level
        threat_match = re.search(r"Threat Level:\s*(\w+)", result)
        if threat_match:
            context["threat_level"] = threat_match.group(1)

        # Extract threat actors from galaxies
        ta_matches = re.findall(r"threat-actor.*?\*\s*([^\n:]+)", result, re.IGNORECASE)
        context["threat_actors"] = list(set(ta_matches))

        # Extract campaigns
        campaign_matches = re.findall(r"campaign.*?\*\s*([^\n:]+)", result, re.IGNORECASE)
        context["campaigns"] = list(set(campaign_matches))

        # Extract MITRE techniques
        mitre_matches = re.findall(r"mitre-attack.*?\*\s*([^\n:]+)", result, re.IGNORECASE)
        context["mitre_techniques"] = list(set(mitre_matches))

        # Extract tags
        tags_match = re.search(r"Tags:\s*([^\n]+)", result)
        if tags_match:
            context["tags"] = [t.strip() for t in tags_match.group(1).split(",")]

        # Extract attribute count
        attr_match = re.search(r"Attributes:\s*(\d+)", result)
        if attr_match:
            context["attribute_count"] = int(attr_match.group(1))

        return context

    except Exception as e:
        logger.error("misp_event_context_failed", event_id=event_id, error=str(e))
        return None


async def _check_warninglist(client: Any, observable: Observable) -> dict[str, Any] | None:
    """Check if an IOC is on MISP warninglists.

    Args:
        client: MISP MCP client.
        observable: Observable to check.

    Returns:
        Warninglist result or None.
    """
    try:
        result = await client.call_tool(
            "check_misp_warninglist",
            {"value": observable.value}
        )

        if not result:
            return None

        # Check if it's on a warninglist
        if "NOT on any warninglist" in result:
            return {
                "value": observable.value,
                "type": observable.type.value,
                "on_warninglist": False,
            }

        if "WARNING" in result or "warninglist" in result.lower():
            # Extract warninglist names
            wl_matches = re.findall(r"-\s*([^(]+)\s*\(ID:", result)

            return {
                "value": observable.value,
                "type": observable.type.value,
                "on_warninglist": True,
                "warninglists": [w.strip() for w in wl_matches],
                "raw_result": result[:300],
            }

        return None

    except Exception as e:
        logger.warning("misp_warninglist_check_failed", error=str(e))
        return None


def _generate_misp_findings(
    matches: list[dict],
    threat_actors: list[str],
    campaigns: list[str],
    warninglist_hits: list[dict],
) -> list[dict]:
    """Generate investigation findings from MISP context.

    Args:
        matches: IOC matches from MISP.
        threat_actors: Identified threat actors.
        campaigns: Identified campaigns.
        warninglist_hits: IOCs on warninglists.

    Returns:
        List of finding dictionaries.
    """
    findings = []

    # Finding for IOC matches
    if matches:
        to_ids_matches = [m for m in matches if m.get("to_ids")]

        if to_ids_matches:
            findings.append({
                "description": f"MISP: {len(to_ids_matches)} IOC(s) flagged for IDS detection found in threat intelligence",
                "severity": Severity.HIGH.value,
                "evidence": [
                    f"{m['value']} ({m['type']}) - Events: {', '.join(m.get('event_ids', []))}"
                    for m in to_ids_matches[:5]
                ],
                "recommendations": [
                    "Review MISP event context for attribution",
                    "Consider blocking these IOCs at perimeter",
                    "Search for related indicators in the environment",
                ],
                "source": "misp",
            })
        elif matches:
            findings.append({
                "description": f"MISP: {len(matches)} IOC(s) found in threat intelligence database",
                "severity": Severity.MEDIUM.value,
                "evidence": [
                    f"{m['value']} ({m['type']}) - Events: {', '.join(m.get('event_ids', []))}"
                    for m in matches[:5]
                ],
                "recommendations": [
                    "Review MISP event context for more details",
                    "Assess if IOCs are still relevant",
                ],
                "source": "misp",
            })

    # Finding for threat actor attribution
    if threat_actors:
        findings.append({
            "description": f"MISP: Potential threat actor attribution identified - {', '.join(threat_actors[:3])}",
            "severity": Severity.HIGH.value,
            "evidence": [f"Threat actor: {ta}" for ta in threat_actors[:5]],
            "recommendations": [
                "Review threat actor TTPs in MITRE ATT&CK",
                "Search for other indicators associated with this actor",
                "Consider threat actor targeting and motivation",
            ],
            "source": "misp",
        })

    # Finding for campaign attribution
    if campaigns:
        findings.append({
            "description": f"MISP: IOCs linked to known campaign(s) - {', '.join(campaigns[:3])}",
            "severity": Severity.HIGH.value,
            "evidence": [f"Campaign: {c}" for c in campaigns[:5]],
            "recommendations": [
                "Review campaign timeline and scope",
                "Check for other campaign indicators",
            ],
            "source": "misp",
        })

    # Finding for warninglist hits (false positive indicators)
    if warninglist_hits:
        findings.append({
            "description": f"MISP: {len(warninglist_hits)} IOC(s) found on warninglists - potential false positives",
            "severity": Severity.LOW.value,
            "evidence": [
                f"{h['value']} on: {', '.join(h.get('warninglists', ['unknown']))}"
                for h in warninglist_hits[:5]
            ],
            "recommendations": [
                "Review warninglist matches for false positive assessment",
                "These IOCs may be benign (CDN IPs, common domains, etc.)",
            ],
            "source": "misp",
        })

    return findings
