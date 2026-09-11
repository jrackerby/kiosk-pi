"""The three platforms the entity-surface tests only counted.

``test_init.py`` asserts these entities EXIST. Existence is not behaviour, and
each of the three has one failure mode worth more than its presence:

- the screenshot serving a STALE frame — an operator looking at a board that is
  no longer on the glass, which is worse than a blank;
- the overlay refusing to CLEAR — a wall stuck behind a notice nobody can lift;
- a diagnostics dump carrying a password — remote control of a wall, pasted into
  a public issue.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.notify import (
    ATTR_MESSAGE,
    ATTR_TITLE,
    DOMAIN as NOTIFY_DOMAIN,
    SERVICE_SEND_MESSAGE,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.kiosk_pi.api import KioskPiError
from custom_components.kiosk_pi.const import CONF_PASSWORD
from custom_components.kiosk_pi.coordinator import UPDATE_INTERVAL
from custom_components.kiosk_pi.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import PASSWORD, mock_agent

PNG = b"\x89PNG\r\n\x1a\n first frame"
PNG_2 = b"\x89PNG\r\n\x1a\n second frame"

SCREENSHOT = "custom_components.kiosk_pi.api.KioskPiClient.screenshot"


async def setup(hass: HomeAssistant, entry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def advance(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """One poll interval, drained until the response has actually resolved.

    ONE DRAIN IS NOT ENOUGH on this harness: the first gets as far as the
    request being issued while the mocked response resolves on a later loop
    turn, so the coordinator is still holding its previous data and the test
    reads as a caching bug that is not there.
    """
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    freezer.tick(UPDATE_INTERVAL + timedelta(seconds=1))
    async_fire_time_changed(hass)
    for _ in range(3):
        await hass.async_block_till_done()


async def fetch(hass: HomeAssistant) -> bytes | None:
    entity = hass.data["image"].get_entity("image.office_wall_screenshot")
    return await entity.async_image()


# --- image -------------------------------------------------------------------

async def test_the_screenshot_is_fetched_on_demand_not_on_the_poll(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes, freezer: FrozenDateTimeFactory,
) -> None:
    """A PNG of a 2560x1440 panel is megabytes. Pulling one every thirty
    seconds from four panels would be the integration's entire cost, for an
    image nobody is looking at most of the time."""
    mock_agent(aioclient_mock)
    with patch(SCREENSHOT, return_value=PNG) as captured:
        await setup(hass, config_entry)
        assert captured.call_count == 0

        await advance(hass, freezer)
        assert captured.call_count == 0

        assert await fetch(hass) == PNG
        assert captured.call_count == 1


async def test_a_second_render_between_polls_is_served_from_cache(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    mock_agent(aioclient_mock)
    with patch(SCREENSHOT, return_value=PNG) as captured:
        await setup(hass, config_entry)
        assert await fetch(hass) == PNG
        assert await fetch(hass) == PNG
        assert captured.call_count == 1


async def test_a_refresh_invalidates_the_cached_frame(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes, freezer: FrozenDateTimeFactory,
) -> None:
    """Invalidate, do not re-fetch. The next render pulls a fresh PNG; until
    then nothing crosses the network."""
    mock_agent(aioclient_mock)
    with patch(SCREENSHOT, side_effect=[PNG, PNG_2]) as captured:
        await setup(hass, config_entry)
        assert await fetch(hass) == PNG

        await advance(hass, freezer)
        assert captured.call_count == 1, "the refresh must not itself capture"

        assert await fetch(hass) == PNG_2


async def test_image_last_updated_moves_on_every_refresh(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes, freezer: FrozenDateTimeFactory,
) -> None:
    """It is what makes the frontend re-fetch. It deliberately does NOT track
    the moment the PNG was captured — the entity has no way to know that
    without capturing one, which is the cost this design exists to avoid."""
    mock_agent(aioclient_mock)
    with patch(SCREENSHOT, return_value=PNG):
        await setup(hass, config_entry)
        first = hass.states.get("image.office_wall_screenshot").state

        await advance(hass, freezer)
        assert hass.states.get("image.office_wall_screenshot").state != first


async def test_a_failed_capture_serves_no_image_not_the_previous_frame(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes, freezer: FrozenDateTimeFactory,
) -> None:
    """THE ONE THAT MATTERS. Serving the previous frame would show an operator
    a board that is no longer on the glass, and it would look entirely
    healthy — which is strictly worse than a blank, because a blank is
    self-evidently a failure and a stale board is not.
    """
    mock_agent(aioclient_mock)
    with patch(SCREENSHOT, side_effect=[PNG, KioskPiError("browser is down")]):
        await setup(hass, config_entry)
        assert await fetch(hass) == PNG

        await advance(hass, freezer)
        assert await fetch(hass) is None


async def test_a_capture_that_fails_first_recovers_on_the_next_render(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """A failure is not latched. Nothing caches the absence, so the render
    after the browser comes back serves a real frame."""
    mock_agent(aioclient_mock)
    with patch(SCREENSHOT, side_effect=[KioskPiError("down"), PNG_2]):
        await setup(hass, config_entry)
        assert await fetch(hass) is None
        assert await fetch(hass) == PNG_2


# --- notify ------------------------------------------------------------------

async def send(hass: HomeAssistant, message: str,
               title: str | None = None) -> None:
    data = {ATTR_ENTITY_ID: "notify.office_wall_overlay_message",
            ATTR_MESSAGE: message}
    if title is not None:
        data[ATTR_TITLE] = title
    await hass.services.async_call(
        NOTIFY_DOMAIN, SERVICE_SEND_MESSAGE, data, blocking=True
    )


@pytest.mark.parametrize("message,title,expected", [
    ("Boiler service at 14:00", None, "Boiler service at 14:00"),
    ("Boiler service at 14:00", "Maintenance",
     "Maintenance\nBoiler service at 14:00"),
    # SENDING AN EMPTY MESSAGE CLEARS IT. There is no second entity for that:
    # an overlay with no text is an overlay that is not there.
    ("", None, ""),
])
async def test_the_overlay_text_reaches_the_agent(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes, message, title, expected,
) -> None:
    """TITLE AND MESSAGE ARE JOINED, not rendered separately: the overlay is
    one block of centred text on a wall read from across a room, and a second
    type size in it buys nothing at that distance."""
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)

    with patch(
        "custom_components.kiosk_pi.api.KioskPiClient.set_overlay"
    ) as overlay:
        await send(hass, message, title)

    overlay.assert_called_once_with(expected)


async def test_clearing_the_overlay_is_the_same_call_not_a_second_control(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """A separate "clear" button would be a second control for one state, and
    the two would drift the day one of them stops working."""
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)

    with patch(
        "custom_components.kiosk_pi.api.KioskPiClient.set_overlay"
    ) as overlay:
        await send(hass, "Evacuate via the north door")
        await send(hass, "")

    assert [call.args[0] for call in overlay.call_args_list] == [
        "Evacuate via the north door", ""
    ]


async def test_a_title_with_an_empty_message_still_clears_nothing_silently(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """A title alone is still text on the wall. It must not be dropped on the
    grounds that the message half is empty — the operator sent something, and
    an overlay that shows nothing after a successful call is a lie."""
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)

    with patch(
        "custom_components.kiosk_pi.api.KioskPiClient.set_overlay"
    ) as overlay:
        await send(hass, "", "Maintenance")

    overlay.assert_called_once_with("Maintenance\n")


# --- diagnostics -------------------------------------------------------------

async def test_both_passwords_are_redacted(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """TWO COPIES, NOT ONE. The entry's credential and the agent's own copy of
    it in the settings map are different keys in different sub-trees, and a
    redaction covering only the first ships the secret anyway — in the half of
    the dump nobody thinks to look at.

    Asserted by sweeping the WHOLE serialised dump for the value, not by
    checking the two keys: a key check passes the day the agent starts
    reporting the password under a third name.
    """
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)

    dump = await async_get_config_entry_diagnostics(hass, config_entry)

    assert dump["entry"]["data"][CONF_PASSWORD] != PASSWORD
    assert dump["settings"]["remoteAdminPassword"] != PASSWORD
    assert PASSWORD not in repr(dump)


async def test_the_ssid_is_deliberately_not_redacted(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """It is not a credential, it is the single most useful field for
    diagnosing a panel that roams, and redacting facts that are merely
    identifying makes a dump that cannot answer the question it was collected
    for. Recorded as a test so it reads as a decision rather than an oversight.
    """
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)

    dump = await async_get_config_entry_diagnostics(hass, config_entry)
    assert dump["device_info"]["wifi"]["ssid"] == "Example"


async def test_the_dump_says_whether_the_panel_is_answering(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """A dump collected from a dead panel that does not say the panel was dead
    sends the reader looking for a fault in the integration."""
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)

    dump = await async_get_config_entry_diagnostics(hass, config_entry)
    assert dump["last_update_success"] is True
    assert dump["entry"]["version"] == config_entry.version
