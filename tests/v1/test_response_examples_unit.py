"""The shipped example playbooks must parse against the real schema.

These files are what operators copy from, so an authoring mistake in them is a
mistake in every playbook derived from them. One example shipped for months
passing ``note`` to ``annotate_investigation`` (the handler reads ``body``),
which validated fine because ``params`` is opaque and produced an empty note at
execution time. Capability param allowlists close that hole; this test keeps the
examples themselves honest.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from soctalk.response.models import ResponsePlaybook

EXAMPLES = sorted(
    (Path(__file__).resolve().parents[2] / "examples" / "response-playbooks").glob("*.json")
)


def test_examples_directory_is_not_empty() -> None:
    assert EXAMPLES, "no example response playbooks found"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_playbook_validates(path: Path) -> None:
    ResponsePlaybook.model_validate(json.loads(path.read_text()))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_annotations_carry_a_body(path: Path) -> None:
    """An annotate_investigation action with params but no ``body`` would write
    the fallback note instead of the authored text."""
    playbook = ResponsePlaybook.model_validate(json.loads(path.read_text()))
    actions = list(playbook.response.on_escalate) + list(playbook.response.on_close)
    for action in actions:
        if action.capability == "annotate_investigation" and action.params:
            assert str(action.params.get("body") or "").strip(), (
                f"{path.name}: annotate_investigation has params "
                f"{sorted(action.params)} but no non-empty 'body'"
            )
