"""MCP Tool input-schema parsing across SDK versions (issue #111). DB-free.

The orchestrator image installs ``mcp`` unpinned, so the shipped SDK can be
1.x (``Tool.inputSchema``) or 2.x (``Tool.input_schema``). Hard-coding one
attribute silently disabled ALL Wazuh MCP enrichment on 2.x images: the
worker's client raised ``'Tool' object has no attribute 'inputSchema'`` while
listing tools, bound zero tools, and every triage ran without enrichment.
"""

from __future__ import annotations

from types import SimpleNamespace

from soctalk.mcp.client import _tool_input_schema


def test_reads_camelcase_schema_mcp_1x():
    tool = SimpleNamespace(name="t", inputSchema={"type": "object", "a": 1})
    assert _tool_input_schema(tool) == {"type": "object", "a": 1}


def test_reads_snakecase_schema_mcp_2x():
    # mcp 2.0.0 renamed the attribute; the object has NO ``inputSchema``.
    tool = SimpleNamespace(name="t", input_schema={"type": "object", "b": 2})
    assert not hasattr(tool, "inputSchema")
    assert _tool_input_schema(tool) == {"type": "object", "b": 2}


def test_missing_schema_is_empty_dict_not_raise():
    tool = SimpleNamespace(name="t")
    assert _tool_input_schema(tool) == {}


def test_camelcase_wins_when_both_present():
    tool = SimpleNamespace(
        name="t", inputSchema={"canonical": True}, input_schema={"legacy": True}
    )
    assert _tool_input_schema(tool) == {"canonical": True}
