"""Model-ID normalization, shared by every pricing path (#139).

Providers return versioned model IDs — ``claude-haiku-4-5-20251001``,
``gpt-4o-2024-08-06``, ``claude-3-5-sonnet-latest`` — while price tables are
keyed by family. Pinning a dated ID is the recommended practice, so the
versioned form is what a careful deployment actually runs.

This lived only in ``graph/budget.py``. The catalog path added in 0.2.1 looked
up the raw ID with an exact match and therefore missed every dated ID, resolving
``unknown`` and billing at the fail-expensive fallback — measured at ~13x on a
real run against ``claude-haiku-4-5-20251001``. One definition, used by both.
"""

from __future__ import annotations

import re

# Trailing ``-YYYYMMDD`` (Anthropic style), ``-YYYY-MM-DD`` (OpenAI style), or
# the literal ``-latest`` alias.
#
# ONLY these known suffix shapes are stripped, never a free-form trailing
# token: ``gpt-4-32k`` and ``gpt-4-vision`` are *different SKUs* at different
# prices, and folding them into ``gpt-4`` would bill a customer for the wrong
# model. A missed strip over-charges and halts early, which is visible; a wrong
# strip under-charges silently, which is not.
VERSION_SUFFIX_RE = re.compile(r"(?:-(?:\d{8}|\d{4}-\d{2}-\d{2})|-latest)$")


def base_model_id(model: str | None) -> str:
    """The family ID for ``model``, with any version/date suffix removed.

    Returns "" for a falsy input. Idempotent: a base ID passes through
    unchanged, so callers can apply it without checking first.

        claude-haiku-4-5-20251001  -> claude-haiku-4-5
        claude-3-5-sonnet-latest   -> claude-3-5-sonnet
        gpt-4o-2024-08-06          -> gpt-4o
        gpt-4-32k                  -> gpt-4-32k   (different SKU, untouched)
    """
    if not model:
        return ""
    return VERSION_SUFFIX_RE.sub("", model, count=1)
