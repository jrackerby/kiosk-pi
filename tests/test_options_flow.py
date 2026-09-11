"""The options flow's three branches — and the merge that fails silently.

THE MERGE IS WHY THIS FILE EXISTS. ``async_create_entry(data=...)`` REPLACES
``entry.options`` wholesale, so a step returning only its own keys deletes every
other step's, with no exception, no log line and no edit to point at. It is
harmless while a flow has one step — which is exactly how it survives to the
commit that adds the second — and this flow has three.

A TEST THAT WALKS ONE STEP CANNOT CATCH IT. The deletion is only observable
across two steps against the same entry, so every test here that touches the
merge seeds a key the step under test does not know about and asserts it
SURVIVED. Asserting only that the step's own key landed passes against the
broken form.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.kiosk_pi.const import (
    CONF_ALLOW_CONTROL_BOARDS,
    CONF_OFFLINE_EXPECTED,
)
from custom_components.kiosk_pi.external_apps import async_get_apps

from .conftest import mock_agent


async def setup(hass: HomeAssistant, entry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def open_menu(hass: HomeAssistant, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    return result


# --- the menu ----------------------------------------------------------------

async def test_the_menu_offers_all_three_branches(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)

    result = await open_menu(hass, config_entry)
    assert set(result["menu_options"]) == {"panel", "add_app", "remove_app"}


# --- the merge ---------------------------------------------------------------

@pytest.mark.parametrize("step,user_input", [
    ("panel", {CONF_ALLOW_CONTROL_BOARDS: True, CONF_OFFLINE_EXPECTED: False}),
    ("add_app", {"key": "ops", "url": "http://boards.invalid/ops/",
                 "surface": "monitor"}),
])
async def test_a_step_does_not_delete_another_steps_keys(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes, step, user_input,
) -> None:
    """The silent one. Every branch merges over what is already there.

    `foreign_key` stands for a key some OTHER step of this flow owns. No step
    under test knows about it, so a step that returns `data=user_input` — or
    `data={}` — drops it, and nothing anywhere reports that it did.
    """
    hass.config_entries.async_update_entry(
        config_entry, options={"foreign_key": "set-by-another-step"}
    )
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)

    result = await open_menu(hass, config_entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options["foreign_key"] == "set-by-another-step"


async def test_remove_app_does_not_delete_another_steps_keys(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """Same assertion, but this branch needs an app to exist first.

    It aborts on an empty list, so it cannot be reached from the parametrised
    case above without seeding one — and an abort would pass a merge assertion
    without ever running the write.
    """
    hass.config_entries.async_update_entry(
        config_entry, options={"foreign_key": "set-by-another-step"}
    )
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)

    await _add_app(hass, config_entry, key="ops",
                   url="http://boards.invalid/ops/")

    result = await open_menu(hass, config_entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_app"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"key": "ops"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options["foreign_key"] == "set-by-another-step"


# --- the panel branch --------------------------------------------------------

async def test_the_panel_branch_stores_its_own_options(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)

    result = await open_menu(hass, config_entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "panel"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_ALLOW_CONTROL_BOARDS: True, CONF_OFFLINE_EXPECTED: True},
    )
    await hass.async_block_till_done()

    assert config_entry.options[CONF_ALLOW_CONTROL_BOARDS] is True
    assert config_entry.options[CONF_OFFLINE_EXPECTED] is True


# --- the app list ------------------------------------------------------------

async def _add_app(hass: HomeAssistant, entry, *, key: str, url: str,
                   surface: str = "monitor") -> None:
    result = await open_menu(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_app"}
    )
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"key": key, "url": url, "surface": surface}
    )
    await hass.async_block_till_done()


async def test_add_app_persists_the_app(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)

    await _add_app(hass, config_entry, key="ops",
                   url="http://boards.invalid/ops/")

    assert await async_get_apps(hass) == [
        {"key": "ops", "url": "http://boards.invalid/ops/",
         "surface": "monitor"}
    ]


async def test_adding_a_key_that_exists_is_an_edit_not_a_duplicate(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """Two apps at one key would leave the picker and the probe disagreeing
    about which URL that key means."""
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)

    await _add_app(hass, config_entry, key="ops",
                   url="http://boards.invalid/ops/")
    await _add_app(hass, config_entry, key="ops",
                   url="http://boards.invalid/ops-v2/", surface="control")

    apps = await async_get_apps(hass)
    assert apps == [
        {"key": "ops", "url": "http://boards.invalid/ops-v2/",
         "surface": "control"}
    ]


async def test_remove_app_aborts_when_there_is_nothing_to_remove(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """An abort, not a form with an empty picker.

    A `vol.In([])` selector renders a control that cannot be satisfied, so the
    operator's only way out of the step is the back button.
    """
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)

    result = await open_menu(hass, config_entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_app"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_apps"


async def test_remove_app_removes_only_the_named_app(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)

    await _add_app(hass, config_entry, key="ops",
                   url="http://boards.invalid/ops/")
    await _add_app(hass, config_entry, key="wall",
                   url="http://boards.invalid/wall/")

    result = await open_menu(hass, config_entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_app"}
    )
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"key": "ops"}
    )
    await hass.async_block_till_done()

    assert [app["key"] for app in await async_get_apps(hass)] == ["wall"]


async def test_the_dashboard_picker_is_refreshed_by_both_app_branches(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """The store is not the surface. Without the refresh the select entity
    keeps offering the list it cached at setup, so an app added through this
    flow is absent from the only control that can load it.

    PATCHED ON `config_flow`'s OWN NAME, not on `discovery`'s. `config_flow`
    binds `async_refresh_cache` at import, so the `no_app_probes` fixture —
    which patches the attribute on `discovery` — does not reach this call site
    at all. A test asserting through that fixture would be asserting on a mock
    nothing calls.
    """
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)

    with patch(
        "custom_components.kiosk_pi.config_flow.async_refresh_cache",
        return_value=[],
    ) as refreshed:
        await _add_app(hass, config_entry, key="ops",
                       url="http://boards.invalid/ops/")
        assert refreshed.call_count == 1

        result = await open_menu(hass, config_entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "remove_app"}
        )
        await hass.config_entries.options.async_configure(
            result["flow_id"], {"key": "ops"}
        )
        await hass.async_block_till_done()
        assert refreshed.call_count == 2
