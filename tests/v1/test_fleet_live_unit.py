"""Unit tests for fleet-live stage mapping (issue #72). DB-free."""

from __future__ import annotations

import inspect
import zoneinfo
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from soctalk.core.api import fleet_day as fleet_day_mod
from soctalk.core.api.fleet_day import (
    _hour_histogram,
    _resolve_day_window,
    stage_for_latest_event,
)


def test_stage_mapping_covers_all_replay_kinds():
    assert stage_for_latest_event("alert_ingested", {}) == "gate"
    assert stage_for_latest_event("policy_resolved", {}) == "gate"
    assert stage_for_latest_event("supervisor_decision", {}) == "sup"
    assert stage_for_latest_event("worker_started", {"worker": "wazuh"}) == "wazuh"
    assert stage_for_latest_event("worker_started", {"worker": "cortex"}) == "cortex"
    assert stage_for_latest_event("worker_started", {"worker": "misp"}) == "misp"
    assert (
        stage_for_latest_event("worker_started", {"worker": "authorization_context"})
        == "authz"
    )
    assert stage_for_latest_event("worker_result", {"worker": "wazuh"}) == "sup"
    assert stage_for_latest_event("budget_warning", {}) == "sup"
    assert stage_for_latest_event("verdict_rendered", {}) == "verdict"
    assert stage_for_latest_event("guard_evaluated", {"effect": "pass"}) == "guard"
    assert stage_for_latest_event("human_review_requested", {}) == "human"
    assert stage_for_latest_event("human_decision", {}) == "human"
    assert stage_for_latest_event("auto_closed", {}) == "close"


def test_stage_mapping_is_honest_about_unknowns():
    assert stage_for_latest_event("hypothesis_updated", {}) == "unknown"
    assert stage_for_latest_event("nonsense_kind", None) == "unknown"
    assert stage_for_latest_event("worker_started", {"worker": "not-a-worker"}) == "sup"


def test_resolve_day_window_legacy_alias_via_tzdata_package():
    """A browser reporting a legacy backward-compat zone (e.g. the Argentina
    alias America/Buenos_Aires) must resolve even when the ONLY tz source is the
    first-party ``tzdata`` package. That is the slim-container case: system
    /usr/share/zoneinfo omits the ``backward`` links, so without the tzdata
    dependency fleet-day 400s "unknown timezone" and blanks the whole panel.

    We empty TZPATH to force the tzdata-package lookup, reproducing the container
    on any host (macOS/Linux system tzdata would otherwise mask the regression).
    """
    saved = tuple(zoneinfo.TZPATH)
    try:
        ZoneInfo.clear_cache()
        zoneinfo.reset_tzpath(to=[])  # no system tz dirs; only the tzdata package
        day, start, end = _resolve_day_window("America/Buenos_Aires", date(2026, 7, 15))
        assert day == date(2026, 7, 15)
        # Buenos Aires has been UTC-3 with no DST since 2009, so this fixed
        # winter date is a stable offset check independent of "today".
        assert start.utcoffset() == timedelta(hours=-3)
        assert end - start == timedelta(days=1)
    finally:
        zoneinfo.reset_tzpath(to=list(saved))
        ZoneInfo.clear_cache()


def test_resolve_day_window_rejects_truly_unknown_tz():
    """A genuinely bogus zone still fails closed with a 400, not a 500."""
    with pytest.raises(HTTPException) as ei:
        _resolve_day_window("Mars/Olympus_Mons", None)
    assert ei.value.status_code == 400


def test_hour_histogram_buckets_in_local_zone_via_tzdata():
    """The fleet-day histogram buckets in Python (not SQL AT TIME ZONE) because
    Postgres rejects legacy tz aliases that tzdata resolves. Verify aware UTC
    timestamps land in the correct LOCAL hour of a legacy-aliased zone, with
    TZPATH emptied so only the tzdata package can supply it (container case).
    """
    saved = tuple(zoneinfo.TZPATH)
    try:
        ZoneInfo.clear_cache()
        zoneinfo.reset_tzpath(to=[])
        zone = ZoneInfo("America/Buenos_Aires")  # UTC-3, legacy alias
        ts = [
            datetime(2026, 7, 15, 2, 30, tzinfo=UTC),   # 23:30 local -> 23
            datetime(2026, 7, 15, 3, 15, tzinfo=UTC),   # 00:15 local -> 0
            datetime(2026, 7, 15, 12, 0, tzinfo=UTC),   # 09:00 local -> 9
            datetime(2026, 7, 15, 12, 59, tzinfo=UTC),  # 09:59 local -> 9
        ]
        hist = _hour_histogram(ts, zone)
        assert len(hist) == 24
        assert hist[23] == 1
        assert hist[0] == 1
        assert hist[9] == 2
        assert sum(hist) == 4
    finally:
        zoneinfo.reset_tzpath(to=list(saved))
        ZoneInfo.clear_cache()


def test_fleet_day_histogram_stays_out_of_postgres_tz_conversion():
    """End-to-end tripwire for the Postgres-alias failure mode: the fleet-day
    endpoint must bucket the histogram in Python (_hour_histogram), never via
    SQL ``AT TIME ZONE :tz`` — postgres:16's catalog rejects legacy aliases like
    America/Buenos_Aires, so reintroducing that SQL would 500 the panel again.
    """
    src = inspect.getsource(fleet_day_mod.fleet_day)
    assert "AT TIME ZONE :tz" not in src
    assert "_hour_histogram(" in src
