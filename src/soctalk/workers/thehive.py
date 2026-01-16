"""TheHive worker node for incident response operations."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig

from soctalk.mcp.bindings import get_thehive_client
from soctalk.models.enums import Phase, InvestigationStatus, Severity
from soctalk.models.investigation import Investigation
from soctalk.persistence.emitter import get_emitter_from_config, get_investigation_id_from_state

logger = structlog.get_logger()


async def thehive_worker_node(
    state: dict[str, Any],
    run_config: RunnableConfig | None = None,
) -> dict[str, Any]:
    """TheHive worker node - handles incident response operations.

    This worker can:
    - Check for existing cases/alerts
    - Create new cases
    - Promote alerts to cases

    Args:
        state: Current graph state.

    Returns:
        Updated state dictionary.
    """
    logger.info("thehive_worker_started")

    client = get_thehive_client()
    investigation_data = state.get("investigation", {})

    # Reconstruct Investigation object
    investigation = Investigation(**investigation_data) if isinstance(investigation_data, dict) else investigation_data

    # Get emitter for event emission
    emitter = get_emitter_from_config(run_config)
    investigation_id = get_investigation_id_from_state(state)

    try:
        # Create the case in TheHive
        case_id = await _create_case(client, investigation)

        if case_id:
            investigation.thehive_case_id = case_id
            investigation.status = InvestigationStatus.ESCALATED
            logger.info("case_created", case_id=case_id)

            # Emit thehive case created event
            if emitter and investigation_id:
                try:
                    await emitter.emit_thehive_case_created(
                        investigation_id=investigation_id,
                        case_id=case_id,
                        case_number=None,  # Could extract from case_id if pattern known
                        title=investigation.title,
                        idempotency_key=f"thehive-case-{investigation_id}-{case_id}",
                    )
                except Exception as emit_error:
                    logger.warning("event_emission_failed", error=str(emit_error))
        else:
            logger.warning("case_creation_failed")
            state["last_error"] = "Failed to create TheHive case"

        state["investigation"] = investigation.model_dump()
        state["current_phase"] = Phase.CLOSED.value

    except Exception as e:
        logger.error("thehive_worker_error", error=str(e))
        state["last_error"] = f"TheHive worker error: {str(e)}"
        state["error_count"] = state.get("error_count", 0) + 1

    state["last_updated"] = datetime.now().isoformat()
    return state


async def _create_case(client: Any, investigation: Investigation) -> str | None:
    """Create a case in TheHive.

    Args:
        client: TheHive MCP client.
        investigation: Investigation to create case from.

    Returns:
        Case ID if created, None otherwise.
    """
    # Generate case data from investigation
    case_data = investigation.to_thehive_case_data()

    logger.info(
        "creating_thehive_case",
        title=case_data["title"],
        severity=case_data["severity"],
    )

    try:
        result = await client.call_tool(
            "create_thehive_case",
            {
                "title": case_data["title"],
                "description": case_data["description"],
                "severity": case_data["severity"],
                "tags": case_data["tags"],
                "tlp": case_data["tlp"],
                "pap": case_data["pap"],
            }
        )

        if result:
            # Try to extract case ID from result
            case_id = _extract_case_id(result)

            # If case was created successfully, add observables
            if case_id:
                await _add_observables_to_case(client, case_id, investigation)

            return case_id

        return None

    except Exception as e:
        logger.error("failed_to_create_case", error=str(e))
        raise


async def _add_observables_to_case(
    client: Any, case_id: str, investigation: Investigation
) -> None:
    """Add observables to a TheHive case.

    Args:
        client: TheHive MCP client.
        case_id: ID of the case to add observables to.
        investigation: Investigation containing observables.
    """
    from soctalk.models.enums import ObservableType, Verdict

    # Map soctalk observable types to TheHive data types
    type_mapping = {
        ObservableType.IP: "ip",
        ObservableType.DOMAIN: "domain",
        ObservableType.URL: "url",
        ObservableType.HASH_MD5: "hash",
        ObservableType.HASH_SHA1: "hash",
        ObservableType.HASH_SHA256: "hash",
        ObservableType.EMAIL: "mail",
        ObservableType.FILENAME: "filename",
        ObservableType.FQDN: "fqdn",
        ObservableType.USER: "other",
        ObservableType.PROCESS: "other",
        ObservableType.REGISTRY_KEY: "registry",
        ObservableType.UNKNOWN: "other",
    }

    # Track added observables to avoid duplicates
    added_values = set()

    for observable in investigation.observables:
        if observable.value in added_values:
            continue

        thehive_type = type_mapping.get(observable.type, "other")

        # Check if this observable has enrichment results
        enrichment = next(
            (e for e in investigation.enrichments if e.observable.value == observable.value),
            None
        )

        # Determine if it's an IOC based on enrichment verdict
        is_ioc = False
        message = observable.context or ""

        if enrichment:
            is_ioc = enrichment.verdict in (Verdict.MALICIOUS, Verdict.SUSPICIOUS)
            if enrichment.verdict == Verdict.MALICIOUS:
                message = f"[MALICIOUS] {message} - {enrichment.analyzer}: {enrichment.verdict.value}"
            elif enrichment.verdict == Verdict.SUSPICIOUS:
                message = f"[SUSPICIOUS] {message} - {enrichment.analyzer}: {enrichment.verdict.value}"

        # Build tags
        tags = list(observable.tags) if observable.tags else []
        tags.append(f"source:{observable.source}")
        if enrichment:
            tags.append(f"verdict:{enrichment.verdict.value}")
            tags.append(f"analyzer:{enrichment.analyzer}")

        try:
            await client.call_tool(
                "create_case_observable",
                {
                    "case_id": case_id,
                    "data_type": thehive_type,
                    "data": observable.value,
                    "message": message.strip() if message else None,
                    "ioc": is_ioc,
                    "sighted": True,
                    "tags": tags,
                }
            )
            added_values.add(observable.value)
            logger.debug(
                "observable_added_to_case",
                case_id=case_id,
                observable=observable.value,
                type=thehive_type,
            )
        except Exception as e:
            logger.warning(
                "failed_to_add_observable",
                case_id=case_id,
                observable=observable.value,
                error=str(e),
            )

    logger.info(
        "observables_added_to_case",
        case_id=case_id,
        count=len(added_values),
    )


def _extract_case_id(result: str) -> str | None:
    """Extract case ID from TheHive response.

    Args:
        result: Raw response from TheHive.

    Returns:
        Case ID or None.
    """
    import re

    # Try to parse as JSON first
    try:
        if result.strip().startswith("{"):
            parsed = json.loads(result)
            return parsed.get("_id") or parsed.get("id") or parsed.get("caseId")
    except json.JSONDecodeError:
        pass

    # Try regex patterns
    patterns = [
        r"Case ID:\s*([^\s\n]+)",
        r"_id[\"']?\s*:\s*[\"']?([^\"'\s,}]+)",
        r"case[_-]?id[\"']?\s*:\s*[\"']?([^\"'\s,}]+)",
        r"#(\d+)",  # Case number
    ]

    for pattern in patterns:
        match = re.search(pattern, result, re.IGNORECASE)
        if match:
            return match.group(1)

    # If result looks like a simple ID
    if result and len(result.strip()) < 50 and not " " in result.strip():
        return result.strip()

    return None


async def check_existing_cases(title_pattern: str) -> list[dict[str, Any]]:
    """Check for existing cases matching a pattern.

    Args:
        title_pattern: Pattern to search in case titles.

    Returns:
        List of matching cases.
    """
    client = get_thehive_client()

    try:
        result = await client.call_tool(
            "get_thehive_cases",
            {"limit": 50}
        )

        if not result:
            return []

        # Parse cases and filter by title
        cases = _parse_cases(result)
        matching = [
            c for c in cases
            if title_pattern.lower() in c.get("title", "").lower()
        ]

        return matching

    except Exception as e:
        logger.error("failed_to_check_cases", error=str(e))
        return []


def _parse_cases(result: str) -> list[dict[str, Any]]:
    """Parse cases from TheHive response.

    Args:
        result: Raw response from TheHive.

    Returns:
        List of case dictionaries.
    """
    cases = []

    # Try JSON array
    try:
        if result.strip().startswith("["):
            return json.loads(result)
    except json.JSONDecodeError:
        pass

    # Parse text format
    # Format: Case #X: Title\n  ID: xxx\n  Status: xxx\n ...
    current_case: dict[str, Any] = {}

    for line in result.split("\n"):
        line = line.strip()

        if line.startswith("Case #") or line.startswith("Case:"):
            if current_case:
                cases.append(current_case)
            current_case = {"title": line.split(":", 1)[-1].strip() if ":" in line else line}

        elif ":" in line and current_case:
            key, value = line.split(":", 1)
            key = key.strip().lower().replace(" ", "_")
            current_case[key] = value.strip()

    if current_case:
        cases.append(current_case)

    return cases


async def get_case_details(case_id: str) -> dict[str, Any] | None:
    """Get details of a specific case.

    Args:
        case_id: TheHive case ID.

    Returns:
        Case details or None.
    """
    client = get_thehive_client()

    try:
        result = await client.call_tool(
            "get_thehive_case_by_id",
            {"case_id": case_id}
        )

        if result:
            # Try to parse
            try:
                if result.strip().startswith("{"):
                    return json.loads(result)
            except json.JSONDecodeError:
                pass

            # Return as text
            return {"raw": result}

        return None

    except Exception as e:
        logger.error("failed_to_get_case", case_id=case_id, error=str(e))
        return None
