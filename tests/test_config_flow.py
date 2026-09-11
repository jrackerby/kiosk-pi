"""The config flow — the quality scale's `config-flow-test-coverage` rule.

EVERY PATH THROUGH THE FLOW, including the refusals. A flow whose happy path is
tested and whose error branches are not is a flow that reports `unknown` for a
wrong password, which sends an operator to check the network.
"""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.kiosk_pi.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    DOMAIN,
)

from .conftest import BASE, DEVICE_INFO, HOST, PASSWORD, PORT, mock_agent

USER_INPUT = {CONF_HOST: HOST, CONF_PASSWORD: PASSWORD, CONF_PORT: PORT}


async def test_user_flow_creates_an_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_app_probes
) -> None:
    mock_agent(aioclient_mock)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Named from the panel's own report, not from the address it was typed at.
    assert result["title"] == "Office Wall"
    assert result["data"][CONF_HOST] == HOST


async def test_unique_id_is_the_panels_hostname_not_its_address(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_app_probes
) -> None:
    """A DHCP lease moving must not let one panel be added twice.

    Keying on the address would split one machine's history across two devices
    with nothing in either to say they are the same wall.
    """
    mock_agent(aioclient_mock)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.unique_id == "kiosk-panel-1"


async def test_a_second_add_of_the_same_panel_aborts_and_updates_the_address(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    # The panel answering at a NEW address — a DHCP lease that moved. It
    # reports the same hostname, which is what makes it the same panel.
    moved = f"http://192.0.2.11:{PORT}/"
    aioclient_mock.get(f"{moved}?cmd=status",
                       json={"status": "OK", "agentVersion": "1.0.0"})
    aioclient_mock.get(f"{moved}?cmd=deviceInfo", json=DEVICE_INFO)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_HOST: "192.0.2.11"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert config_entry.data[CONF_HOST] == "192.0.2.11"


async def test_wrong_password_is_invalid_auth_not_unknown(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{BASE}?cmd=status", status=401,
                       json={"status": "Error", "statustext": "unauthorised"})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_remote_admin_disabled_is_also_invalid_auth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{BASE}?cmd=status", status=403,
                       json={"status": "Error",
                             "statustext": "remote administration is disabled"})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_unreachable_host_is_cannot_connect(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{BASE}?cmd=status", exc=TimeoutError())
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_the_setup_probe_uses_the_channel_the_integration_will_use(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_app_probes
) -> None:
    """A check on a different channel certifies nothing, and not green.

    Asserted on the wire: the probe must reach the agent's own port with the
    password in the Authorization header — the same request the coordinator
    will make — rather than proving reachability some cheaper way.
    """
    mock_agent(aioclient_mock)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    await hass.async_block_till_done()

    method, url, _data, headers = aioclient_mock.mock_calls[0]
    assert method == "GET"
    assert url.host == HOST
    assert url.port == PORT
    assert headers["Authorization"] == f"Bearer {PASSWORD}"
    # And the password is NOT in the URL, where it would reach every debug log
    # and every exception's str().
    assert PASSWORD not in str(url)


async def test_dhcp_discovery_offers_a_form_and_does_not_guess_a_password(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Discovery offers; it does not configure.

    The agent's password is generated on the device, so a discovery flow that
    completed on its own would have to be using a shipped default — which would
    mean the fleet has one.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_DHCP},
        data=DhcpServiceInfo(ip=HOST, hostname="kiosk-panel-1",
                             macaddress="00005e005301"),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert not aioclient_mock.mock_calls


async def test_reauth_updates_the_password(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    mock_agent(aioclient_mock)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "a-new-secret"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_PASSWORD] == "a-new-secret"


async def test_reauth_refuses_a_password_the_agent_still_rejects(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry
) -> None:
    aioclient_mock.get(f"{BASE}?cmd=status", status=401,
                       json={"status": "Error", "statustext": "unauthorised"})
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "still-wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert config_entry.data[CONF_PASSWORD] == PASSWORD


async def test_a_pre_1_0_0_entry_is_upgraded_in_place_not_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_app_probes
) -> None:
    """The upgrade documented in async_migrate_entry must be POSSIBLE.

    THIS IS THE BUG THE FIRST HARDWARE INSTALL FOUND. Migration deliberately
    fails a v2 entry and tells the operator to install the agent and add the
    panel again — but the failed entry keeps the panel's unique id, so the
    second step aborted with `already_configured` and the upgrade could not be
    completed at all. The only way through was to delete the v2 entry first,
    which is precisely what migration refuses to do because it takes the
    panel's recorded history with it.

    Both suites were green while that was true: nothing had a v2 entry and a
    user flow in the same test.
    """
    v2 = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="KIOSK-PANEL-1",
        unique_id="kiosk-panel-1",
        data={"host": HOST, "ssh_user": "kiosk", "ssh_key": "/config/.ssh/kiosk_key"},
        options={"allow_install": True},
    )
    v2.add_to_hass(hass)
    entry_id = v2.entry_id

    mock_agent(aioclient_mock)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "upgraded", (
        "a v2 entry must be adopted, not refused as already_configured"
    )

    # SAME ENTRY, so the device and entity registry rows — and the recorded
    # history behind them — are still attached to it.
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    upgraded = entries[0]
    assert upgraded.entry_id == entry_id
    assert upgraded.version == 3
    assert upgraded.data[CONF_PASSWORD] == PASSWORD
    assert upgraded.data[CONF_HOST] == HOST
    # The SSH transport's keys are gone, not left beside the new ones.
    assert "ssh_key" not in upgraded.data
    assert "ssh_user" not in upgraded.data
    # Options the operator set on the old entry survive the upgrade.
    assert upgraded.options.get("allow_install") is True


async def test_the_migration_repair_issue_is_cleared_by_the_upgrade(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_app_probes
) -> None:
    """A repair telling the operator to do what they just did is noise."""
    v2 = MockConfigEntry(
        domain=DOMAIN, version=2, title="KIOSK-PANEL-1", unique_id="kiosk-panel-1",
        data={"host": HOST}, options={},
    )
    v2.add_to_hass(hass)
    ir.async_create_issue(
        hass, DOMAIN, f"agent_migration_{v2.entry_id}",
        is_fixable=False, severity=ir.IssueSeverity.WARNING,
        translation_key="agent_migration",
    )
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"agent_migration_{v2.entry_id}"
    ) is not None

    mock_agent(aioclient_mock)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"agent_migration_{v2.entry_id}"
    ) is None
