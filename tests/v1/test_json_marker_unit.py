"""Some OpenAI-compatible gateways demand the word "json" before honouring
``response_format`` at all (#131).

Measured on NovaRoute, whose DashScope-served Qwen upstream answers:

    400 'messages' must contain the word 'json' in some form, to use
        'response_format' of type 'json_object'

The request never reaches the model, so every structured call fails. Strict
json_schema is still a response_format, so it is rejected the same way — and
that is the mode AUTO picks for an OpenAI-compatible gateway carrying a schema,
which is why the JSON_OBJECT hint alone did not cover it.
"""
from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from soctalk.inference import _has_json_marker, _json_marker_message


def test_marker_is_detected_in_any_casing_or_shape():
    assert _has_json_marker([HumanMessage(content="reply in json")])
    assert _has_json_marker([HumanMessage(content="Respond with ONLY a single JSON object")])
    # Anthropic-style block content, which make_system_message produces.
    assert _has_json_marker(
        [SystemMessage(content=[{"type": "text", "text": "use JSON"}])]
    )


def test_absent_marker_is_reported_absent():
    assert not _has_json_marker([HumanMessage(content="triage this alert")])
    assert not _has_json_marker([])


def test_the_marker_does_not_restate_the_schema():
    """It adds the token and nothing else.

    Strict decoding already enforces the shape; a second natural-language
    description of the fields would compete with it.
    """
    content = _json_marker_message().content
    assert "json" in content.lower()
    assert len(content) < 60
    for leaky in ("field", "properties", "schema", "{"):
        assert leaky not in content.lower()
