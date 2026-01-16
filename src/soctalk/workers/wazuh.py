"""Wazuh worker node for SIEM operations."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog

from soctalk.mcp.bindings import get_wazuh_client
from soctalk.models.alerts import Alert
from soctalk.models.enums import Phase, Severity
from soctalk.models.investigation import Finding
from soctalk.models.state import SecOpsState

logger = structlog.get_logger()


async def wazuh_worker_node(state: dict[str, Any]) -> dict[str, Any]:
    """Wazuh worker node - handles SIEM operations.

    This worker can:
    - Poll alerts from Wazuh
    - Get agent information and forensics
    - Retrieve vulnerability data
    - Search logs

    Args:
        state: Current graph state.

    Returns:
        Updated state dictionary.
    """
    logger.info("wazuh_worker_started")

    client = get_wazuh_client()
    investigation = state.get("investigation", {})
    supervisor_decision = state.get("supervisor_decision", {})
    specific_instructions = supervisor_decision.get("specific_instructions", "") if supervisor_decision else ""

    try:
        # Determine what action to take based on instructions
        if "forensics" in specific_instructions.lower() or "process" in specific_instructions.lower():
            # Get forensic data for affected agents
            state = await _get_agent_forensics(client, state)
        elif "vulnerability" in specific_instructions.lower() or "vuln" in specific_instructions.lower():
            # Get vulnerability data
            state = await _get_vulnerabilities(client, state)
        elif "log" in specific_instructions.lower():
            # Search logs
            state = await _search_logs(client, state)
        else:
            # Default: get agent context for alerts
            state = await _get_agent_context(client, state)

        state["last_error"] = None
        logger.info("wazuh_worker_completed")

    except Exception as e:
        logger.error("wazuh_worker_error", error=str(e))
        state["last_error"] = f"Wazuh worker error: {str(e)}"
        state["error_count"] = state.get("error_count", 0) + 1

    state["last_updated"] = datetime.now().isoformat()
    return state


async def _get_agent_context(client: Any, state: dict[str, Any]) -> dict[str, Any]:
    """Get context about agents involved in the investigation.

    Args:
        client: Wazuh MCP client.
        state: Current state.

    Returns:
        Updated state.
    """
    investigation = state.get("investigation", {})
    alerts = investigation.get("alerts", [])

    if not alerts:
        return state

    # Get unique agent names from alerts
    agent_names = set()
    for alert in alerts:
        source = alert.get("source", {})
        agent_name = source.get("agent_name")
        if agent_name and agent_name != "unknown":
            agent_names.add(agent_name)

    if not agent_names:
        return state

    # Query agent information
    for agent_name in list(agent_names)[:5]:  # Limit to 5 agents
        try:
            result = await client.call_tool(
                "get_wazuh_agents",
                {"status": "active", "name": agent_name, "limit": 1}
            )

            if result:
                # Add agent context to investigation metadata
                metadata = investigation.get("metadata", {})
                agents_info = metadata.get("agents_info", {})
                agents_info[agent_name] = result
                metadata["agents_info"] = agents_info
                investigation["metadata"] = metadata

                logger.info("agent_context_retrieved", agent=agent_name)

        except Exception as e:
            logger.warning("failed_to_get_agent_info", agent=agent_name, error=str(e))

    state["investigation"] = investigation
    return state


async def _get_agent_forensics(client: Any, state: dict[str, Any]) -> dict[str, Any]:
    """Get forensic data (processes, ports) for agents.

    Args:
        client: Wazuh MCP client.
        state: Current state.

    Returns:
        Updated state with forensic findings.
    """
    investigation = state.get("investigation", {})
    metadata = investigation.get("metadata", {})
    agents_info = metadata.get("agents_info", {})

    findings = investigation.get("findings", [])

    # Get agent IDs from metadata
    for agent_name, agent_data in agents_info.items():
        # Parse agent ID from the response
        agent_id = _extract_agent_id(agent_data)
        if not agent_id:
            continue

        # Get running processes
        try:
            processes_result = await client.call_tool(
                "get_wazuh_agent_processes",
                {"agent_id": agent_id, "limit": 50}
            )

            if processes_result:
                # Look for suspicious processes
                suspicious = _analyze_processes(processes_result)
                if suspicious:
                    finding = Finding(
                        description=f"Suspicious processes found on {agent_name}",
                        severity=Severity.MEDIUM,
                        evidence=suspicious[:5],
                        recommendations=["Review process execution", "Check parent process chain"],
                    )
                    findings.append(finding.model_dump())
                    logger.info("suspicious_processes_found", agent=agent_name, count=len(suspicious))

        except Exception as e:
            logger.warning("failed_to_get_processes", agent=agent_name, error=str(e))

        # Get listening ports
        try:
            ports_result = await client.call_tool(
                "get_wazuh_agent_ports",
                {"agent_id": agent_id, "protocol": "tcp", "state": "LISTENING", "limit": 50}
            )

            if ports_result:
                # Look for unusual ports
                unusual = _analyze_ports(ports_result)
                if unusual:
                    finding = Finding(
                        description=f"Unusual listening ports on {agent_name}",
                        severity=Severity.LOW,
                        evidence=unusual[:5],
                        recommendations=["Verify port usage is legitimate"],
                    )
                    findings.append(finding.model_dump())

        except Exception as e:
            logger.warning("failed_to_get_ports", agent=agent_name, error=str(e))

    investigation["findings"] = findings
    state["investigation"] = investigation
    return state


async def _get_vulnerabilities(client: Any, state: dict[str, Any]) -> dict[str, Any]:
    """Get vulnerability data for agents.

    Args:
        client: Wazuh MCP client.
        state: Current state.

    Returns:
        Updated state with vulnerability findings.
    """
    investigation = state.get("investigation", {})
    metadata = investigation.get("metadata", {})
    agents_info = metadata.get("agents_info", {})

    findings = investigation.get("findings", [])

    for agent_name, agent_data in agents_info.items():
        agent_id = _extract_agent_id(agent_data)
        if not agent_id:
            continue

        try:
            # Get critical vulnerabilities
            vuln_result = await client.call_tool(
                "get_wazuh_critical_vulnerabilities",
                {"agent_id": agent_id, "limit": 20}
            )

            if vuln_result and "No" not in vuln_result:
                finding = Finding(
                    description=f"Critical vulnerabilities found on {agent_name}",
                    severity=Severity.HIGH,
                    evidence=[vuln_result[:500]],
                    recommendations=[
                        "Prioritize patching critical vulnerabilities",
                        "Assess if vulnerabilities are being exploited",
                    ],
                )
                findings.append(finding.model_dump())
                logger.info("vulnerabilities_found", agent=agent_name)

        except Exception as e:
            logger.warning("failed_to_get_vulnerabilities", agent=agent_name, error=str(e))

    investigation["findings"] = findings
    state["investigation"] = investigation
    return state


async def _search_logs(client: Any, state: dict[str, Any]) -> dict[str, Any]:
    """Search Wazuh manager logs.

    Args:
        client: Wazuh MCP client.
        state: Current state.

    Returns:
        Updated state with log findings.
    """
    investigation = state.get("investigation", {})

    try:
        # Search for error logs
        error_result = await client.call_tool(
            "get_wazuh_manager_error_logs",
            {"limit": 20}
        )

        if error_result:
            metadata = investigation.get("metadata", {})
            metadata["manager_errors"] = error_result
            investigation["metadata"] = metadata

    except Exception as e:
        logger.warning("failed_to_search_logs", error=str(e))

    state["investigation"] = investigation
    return state


def _extract_agent_id(agent_data: str) -> str | None:
    """Extract agent ID from Wazuh response text.

    Args:
        agent_data: Raw agent data string.

    Returns:
        Agent ID or None.
    """
    # Parse "ID: 001" pattern from response
    import re
    match = re.search(r"ID:\s*(\d+)", agent_data)
    if match:
        return match.group(1).zfill(3)  # Ensure 3-digit format
    return None


def _analyze_processes(processes_text: str) -> list[str]:
    """Analyze processes for suspicious activity.

    Args:
        processes_text: Raw processes text from Wazuh.

    Returns:
        List of suspicious process descriptions.
    """
    suspicious = []
    suspicious_patterns = [
        "powershell", "cmd.exe", "wscript", "cscript", "mshta",
        "certutil", "bitsadmin", "regsvr32", "rundll32",
        "nc", "ncat", "netcat", "curl", "wget",
        "mimikatz", "procdump", "psexec",
    ]

    lines = processes_text.lower().split("\n")
    for line in lines:
        for pattern in suspicious_patterns:
            if pattern in line:
                suspicious.append(f"Suspicious process: {line.strip()[:100]}")
                break

    return suspicious


def _analyze_ports(ports_text: str) -> list[str]:
    """Analyze listening ports for unusual services.

    Args:
        ports_text: Raw ports text from Wazuh.

    Returns:
        List of unusual port descriptions.
    """
    unusual = []
    # Common legitimate ports to ignore
    common_ports = {22, 80, 443, 3306, 5432, 6379, 8080, 8443, 9200}

    import re
    port_pattern = r"Port:\s*(\d+)"

    for match in re.finditer(port_pattern, ports_text):
        port = int(match.group(1))
        if port not in common_ports and port > 1024:
            # Extract context around the port
            start = max(0, match.start() - 50)
            end = min(len(ports_text), match.end() + 50)
            context = ports_text[start:end].strip()
            unusual.append(f"Unusual port {port}: {context}")

    return unusual


async def poll_wazuh_alerts(limit: int = 100) -> list[Alert]:
    """Poll alerts from Wazuh.

    This is a standalone function used by the alert poller.

    Args:
        limit: Maximum number of alerts to retrieve.

    Returns:
        List of parsed alerts.
    """
    client = get_wazuh_client()

    try:
        result = await client.call_tool(
            "get_wazuh_alert_summary",
            {"limit": limit}
        )

        if not result:
            logger.info("no_alerts_returned", result=result)
            return []

        # Parse alerts from the response
        # Each alert starts with "Alert ID:" - split on this pattern
        alerts = []

        # Split by "Alert ID:" but keep the delimiter
        import re
        alert_blocks = re.split(r'(?=Alert ID:)', result)

        for block in alert_blocks:
            block = block.strip()
            if block and block.startswith("Alert ID:"):
                alert = Alert.from_wazuh_response(block)
                if alert:
                    alerts.append(alert)
                    logger.debug("alert_parsed", alert_id=alert.id, description=alert.rule_description[:50])

        logger.info("alerts_polled", count=len(alerts), raw_blocks=len(alert_blocks))
        return alerts

    except Exception as e:
        logger.error("failed_to_poll_alerts", error=str(e), exc_info=True)
        return []
