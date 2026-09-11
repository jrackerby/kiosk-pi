"""The board-filter policy. Pure functions, no Home Assistant."""

from __future__ import annotations

import pytest

from custom_components.kiosk_pi.surface import (
    CONTROL,
    allowed_apps,
    out_of_policy,
    surface_of,
)

APPS = [
    {"key": "alert-monitor", "url": "http://b.invalid/alert/", "surface": "monitor"},
    {"key": "house-panel", "url": "http://b.invalid/panel/", "surface": "control"},
    {"key": "health", "url": "http://b.invalid/health/", "surface": "health"},
    {"key": "mystery", "url": "http://b.invalid/mystery/"},
]


def test_an_undeclared_surface_is_treated_as_control():
    """The RESTRICTIVE default, and it is the right one for a picker.

    A shell that cannot classify a board should still render it; a picker that
    cannot classify a board should not offer it to a screen nobody can touch.
    Same ladder, opposite safe direction, which is why the policy lives here
    and not in the renderer.
    """
    assert surface_of({"key": "x", "url": "y"}) == CONTROL
    assert surface_of({"surface": "not-a-surface"}) == CONTROL


def test_a_url_that_reads_like_a_monitor_is_still_not_classified_from_it():
    """DECLARED, NEVER INFERRED FROM THE URL.

    A pathname ladder classifies by spelling: right until somebody names a
    control board "wall-monitor", at which point it is confidently wrong and
    nothing in the data catches it.
    """
    assert surface_of({"url": "http://b.invalid/wall-monitor/"}) == CONTROL


def test_control_boards_are_filtered_for_a_wall_that_may_not_have_them():
    keys = {app["key"] for app in allowed_apps(APPS, None, allow_control=False)}
    assert keys == {"alert-monitor", "health"}


def test_the_predicate_is_not_control_rather_than_is_monitor():
    """A health board on a wall currently showing one must stay offered.

    An is-monitor test would strip the wall's own current board out of its
    picker.
    """
    keys = {app["key"] for app in allowed_apps(APPS, None, allow_control=False)}
    assert "health" in keys


def test_allowing_control_offers_everything():
    keys = {app["key"] for app in allowed_apps(APPS, None, allow_control=True)}
    assert keys == {"alert-monitor", "house-panel", "health", "mystery"}


def test_a_wall_already_on_a_control_board_keeps_it_in_its_options():
    """Otherwise the select reads `unknown` and looks like a dead pointer.

    The honest state is to show where the wall IS and let the attribute say the
    position is one policy would not have offered.
    """
    permitted = allowed_apps(APPS, "http://b.invalid/panel/", allow_control=False)
    assert any(app["key"] == "house-panel" for app in permitted)
    assert out_of_policy(APPS, "http://b.invalid/panel/", allow_control=False)


def test_a_board_that_no_longer_exists_is_not_resurrected_by_being_current():
    """A deleted board is absent from `apps`, so it stays absent here.

    Offering it would hide a dead pointer behind a valid-looking option.
    """
    permitted = allowed_apps(APPS, "http://b.invalid/deleted/",
                             allow_control=False)
    assert not any(app["url"] == "http://b.invalid/deleted/" for app in permitted)


def test_an_unknown_url_is_not_a_policy_violation():
    """A panel on an outage page is not on a control board.

    Flagging every unrecognised URL would light this attribute on every wall
    the moment a board server went down.
    """
    assert not out_of_policy(APPS, "http://ha.invalid/outage.html",
                             allow_control=False)


def test_the_assertions_above_can_fail():
    with pytest.raises(AssertionError):
        assert surface_of({"url": "http://b.invalid/wall-monitor/"}) == "monitor"
    with pytest.raises(AssertionError):
        keys = {a["key"] for a in allowed_apps(APPS, None, allow_control=False)}
        assert "house-panel" in keys
