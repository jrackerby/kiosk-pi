"""The board-filter policy: what a given wall may be pointed at.

IMPORTS NOTHING FROM ``homeassistant``. A rule about what a wall may show
should be testable without a Home Assistant, a Pi or a browser.

WHY A WALL IS NOT OFFERED EVERY BOARD. An ambient wall panel is a screen with
no keyboard, mouse or touch, so a control surface on it is unreachable: a hover
never fires, and anything behind a tap is hidden for ever. Offering one in the
picker is offering a dead end. The filter lives in the integration rather than
in a card because a card-side guard leaves the board reachable from more-info,
from an automation and from voice.

THE PREDICATE IS "NOT CONTROL", NOT "IS MONITOR". Monitor, ambient, health and
anything else non-control are all legitimate on a wall; only control is not.
An is-monitor test would strip a health board out of the picker of the very
wall currently showing one.

AN UNCLASSIFIABLE APP IS TREATED AS CONTROL, i.e. filtered out. That is the
restrictive default, and it is the right one HERE even though the shell that
renders these boards defaults the other way for its own purposes: a shell that
cannot classify a board should still render it, and a picker that cannot
classify a board should not offer it to a screen nobody can touch.
"""

from __future__ import annotations

from typing import Any

SURFACES = ("control", "monitor", "health", "ambient")
CONTROL = "control"


def surface_of(app: dict[str, Any]) -> str:
    """The app's declared surface class, defaulting to control.

    DECLARED, NEVER INFERRED FROM THE URL. A pathname ladder guessing from
    words like "panel" or "monitor" classifies by spelling: it is right until
    somebody names a control board "wall-monitor", at which point it is
    confidently wrong and there is nothing in the data to catch it.
    """
    value = str(app.get("surface") or "").strip().lower()
    return value if value in SURFACES else CONTROL


def allowed_apps(apps: list[dict[str, Any]], current: str | None,
                 allow_control: bool) -> list[dict[str, Any]]:
    """The single accessor for what this wall may be pointed at.

    LAW: a config key read by two code paths goes through one accessor. Both
    the select's ``options`` and its ``async_select_option`` call this — a
    guard on the option list alone would leave every other board reachable
    from more-info, an automation or voice, which is exactly what putting the
    filter in the integration is for.

    THE CURRENT TARGET IS ALWAYS KEPT IF IT IS STILL LIVE, and that is not a
    hole in the policy. Dropping a board the wall is demonstrably ON would make
    the select read ``unknown``, which is indistinguishable from a dead
    pointer — the honest state is to show where it is and let the entity's
    ``target_out_of_policy`` attribute say the position is not one this wall
    should be in. A board that no longer exists is absent from ``apps`` and so
    is still absent here.
    """
    if allow_control:
        return list(apps)
    out = [app for app in apps if surface_of(app) != CONTROL]
    if current:
        kept = {app.get("url") for app in out}
        for app in apps:
            if app.get("url") == current and current not in kept:
                out.append(app)
    return out


def out_of_policy(apps: list[dict[str, Any]], current: str | None,
                  allow_control: bool) -> bool:
    """Is the wall currently on a board this policy would not have offered."""
    if allow_control or not current:
        return False
    for app in apps:
        if app.get("url") == current:
            return surface_of(app) == CONTROL
    # A URL that is not a known app is not a policy violation — it is simply
    # not something this integration assigned, and calling it one would flag
    # every panel showing an outage page.
    return False
