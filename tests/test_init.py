"""Setup, unload, migration refusal — and the entity surface the rewrite ships."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.kiosk_pi.const import (
    CONF_HOST,
    CONF_OFFLINE_EXPECTED,
    CONF_PASSWORD,
    CONF_PORT,
    DOMAIN,
)

from .conftest import BASE, DEVICE_INFO, HOST, PASSWORD, PORT, mock_agent


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> bool:
    ok = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return ok


async def test_setup_and_unload(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    mock_agent(aioclient_mock)
    assert await setup_entry(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_retries_when_the_panel_is_unreachable(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """SETUP_RETRY, not a hard failure.

    A wall that is unplugged, rebooting or on a switch that has not come back
    is the normal case for this device class. Failing setup outright would put
    the entry in an error state that needs a human to clear, for a condition
    that clears itself.
    """
    aioclient_mock.get(f"{BASE}?cmd=deviceInfo", exc=TimeoutError())
    aioclient_mock.get(f"{BASE}?cmd=listSettings", exc=TimeoutError())
    await setup_entry(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_a_refused_password_asks_for_reauth_rather_than_retrying(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """A wrong password will still be wrong in thirty seconds.

    Retrying it forever fills the agent's journal with 401s that nobody
    connects back to a Home Assistant entry.
    """
    aioclient_mock.get(f"{BASE}?cmd=deviceInfo", status=401,
                       json={"status": "Error", "statustext": "unauthorised"})
    aioclient_mock.get(f"{BASE}?cmd=listSettings", status=401,
                       json={"status": "Error", "statustext": "unauthorised"})
    await setup_entry(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert any(flow["context"]["source"] == "reauth"
               for flow in hass.config_entries.flow.async_progress())


async def test_an_ssh_era_entry_migrates_its_shape_and_asks_for_the_password(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """REVERSAL, not a refinement, of this test's own earlier contract.

    It used to assert MIGRATION_ERROR, on the reasoning that a migration which
    cannot compute the new credential should fail loudly rather than write an
    entry that cannot connect. The loud failure turned out to be a dead end:
    `migration_error` is NON-RECOVERABLE, so nothing in a running instance can
    move the entry on, and the entry keeps the panel's unique id while it sits
    there — which made the re-add that the failure instructed abort as
    `already_configured`. The upgrade could not be performed at all without
    deleting the entry, which is the history loss the old contract existed to
    prevent. Found on the first real hardware upgrade, the first panel, 2026-09-10.

    The shape IS computable; only the password is not. So migrate the shape and
    let reauth — which Home Assistant has for exactly this — collect the rest.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, title="KIOSK-PANEL-1",
        data={"host": HOST, "ssh_user": "kiosk", "ssh_key": "/config/id",
              "glances_port": 61208},
    )
    entry.add_to_hass(hass)
    await setup_entry(hass, entry)

    # Recoverable, and pointed at the flow that finishes the job.
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.version == 3

    # The SSH transport's keys are gone, not left beside the new ones.
    assert "ssh_user" not in entry.data
    assert "ssh_key" not in entry.data
    assert "glances_port" not in entry.data
    assert entry.data[CONF_HOST] == HOST
    assert entry.data[CONF_PORT] == 2323
    # No invented password: its ABSENCE is what starts reauth.
    assert not entry.data.get(CONF_PASSWORD)

    flows = [
        f for f in hass.config_entries.flow.async_progress()
        if f["context"]["source"] == "reauth"
    ]
    assert len(flows) == 1, "the upgrade must open a reauth flow"

    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"agent_migration_{entry.entry_id}")


async def test_the_migrated_entry_keeps_its_entry_id_and_registry_rows(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Deleting and re-adding would take the panel's recorded history."""
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, title="KIOSK-PANEL-1",
        data={"host": HOST, "ssh_user": "kiosk"},
    )
    entry.add_to_hass(hass)
    entry_id = entry.entry_id
    await setup_entry(hass, entry)

    assert [e.entry_id for e in hass.config_entries.async_entries(DOMAIN)] == [
        entry_id
    ]


async def test_offline_expected_polls_nothing_and_reports_unavailable(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_app_probes
) -> None:
    """A panel deliberately switched off is not a fault.

    Nothing is dialled, so nothing can be inferred — inventing a reading for a
    host that was never contacted is the fail-permissive shape this option
    exists to remove.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, version=3, title="Office Wall", unique_id="kiosk-panel-1",
        data={CONF_HOST: HOST, CONF_PASSWORD: PASSWORD, CONF_PORT: PORT},
        options={CONF_OFFLINE_EXPECTED: True},
    )
    entry.add_to_hass(hass)
    assert await setup_entry(hass, entry)

    assert not aioclient_mock.mock_calls
    state = hass.states.get("binary_sensor.office_wall_agent")
    assert state is not None
    assert state.state == STATE_UNAVAILABLE


async def test_the_device_carries_every_reported_mac(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """Including the cabled interface that is currently down.

    A panel that gets plugged in later should join its own device rather than
    spawn a second one, and the MACs come off the machine rather than off a
    label in another system — a label has merged a device into the wrong
    physical machine before.
    """
    mock_agent(aioclient_mock)
    assert await setup_entry(hass, config_entry)

    devices = dr.async_get(hass)
    device = devices.async_get_device(identifiers={(DOMAIN, config_entry.entry_id)})
    assert device is not None
    macs = {value for kind, value in device.connections
            if kind == dr.CONNECTION_NETWORK_MAC}
    assert macs == {"00:00:5e:00:53:01", "00:00:5e:00:53:02"}
    assert device.sw_version == "pikioskd 1.0.0"


async def test_the_entity_surface(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """Every platform produces entities, and the ids are stable.

    ASSERTED AS A SET, not as a count. A count passes when one entity is
    deleted and another added, which is exactly the change that breaks
    somebody's dashboard silently.
    """
    mock_agent(aioclient_mock)
    assert await setup_entry(hass, config_entry)

    entities = er.async_get(hass)
    ids = {
        entry.entity_id
        for entry in er.async_entries_for_config_entry(
            entities, config_entry.entry_id
        )
    }
    assert ids == {
        "binary_sensor.office_wall_agent",
        "binary_sensor.office_wall_browser",
        "binary_sensor.office_wall_kiosk_mode",
        "binary_sensor.office_wall_screensaver_active",
        "binary_sensor.office_wall_throttled",
        "binary_sensor.office_wall_filesystem_read_only",
        "button.office_wall_load_start_url",
        "button.office_wall_restart_browser",
        "button.office_wall_clear_browser_cache",
        "button.office_wall_clear_cookies",
        "button.office_wall_bring_to_foreground",
        "button.office_wall_restart_device",
        "image.office_wall_screenshot",
        "notify.office_wall_overlay_message",
        "number.office_wall_screen_brightness",
        "number.office_wall_screensaver_brightness",
        "number.office_wall_screen_off_timer",
        "number.office_wall_screensaver_timer",
        "select.office_wall_dashboard",
        "sensor.office_wall_current_page",
        "sensor.office_wall_agent_version",
        "sensor.office_wall_browser_version",
        "sensor.office_wall_browser_restarts",
        "sensor.office_wall_cpu_temperature",
        "sensor.office_wall_free_memory",
        "sensor.office_wall_memory_used",
        "sensor.office_wall_free_storage",
        "sensor.office_wall_wi_fi_signal",
        "sensor.office_wall_resolution",
        "sensor.office_wall_ip_address",
        "sensor.office_wall_uptime",
        "switch.office_wall_screen",
        "switch.office_wall_screensaver",
        "switch.office_wall_kiosk_lock",
        "switch.office_wall_maintenance_mode",
        "text.office_wall_start_url",
        "text.office_wall_outage_page_url",
    }


async def test_unique_ids_are_keyed_on_the_entry_not_the_hostname(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """A panel renamed on the host must not mint a second set of entities.

    Keying on the hostname meant a rename left the original set behind holding
    the history while a new set appeared beside it, with nothing to connect
    them.
    """
    mock_agent(aioclient_mock)
    assert await setup_entry(hass, config_entry)
    entities = er.async_get(hass)
    entry = entities.async_get("sensor.office_wall_current_page")
    assert entry is not None
    assert entry.unique_id == f"{config_entry.entry_id}_current_page"


# --- the upgrade path the operator is actually told to take ------------------
#
# THIS INSTRUCTION HAS BEEN WRONG THREE TIMES, each time because the behaviour
# moved and the text did not: first "re-add, then delete the old entry" when
# deleting destroyed the history; then "add the panel again" when the entry in
# migration_error refused the second step; now, after the entry migrates
# cleanly, "add the panel again" aborts as already_configured instead. Nothing
# tied the words to the code, so nothing caught any of it.
#
# These two tests are that tie. The first pins the real path; the second pins
# the dead one, so the text can be checked against a fact rather than a memory.

async def test_a_migrated_entry_asks_for_reauth_and_keeps_its_identity(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_app_probes
) -> None:
    """The entry must land RECOVERABLE, with the reauth flow already up.

    `migration_error` is non-recoverable — nothing in a running instance moves
    an entry out of it — so a migration that fails is a dead end whatever the
    repair issue says. Asserted on the state, not on the return value.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, title="KIOSK-PANEL-1",
        unique_id="kiosk-panel-1",
        data={"host": HOST, "ssh_user": "kiosk", "ssh_key": "/config/id"},
    )
    entry.add_to_hass(hass)
    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.state is not ConfigEntryState.MIGRATION_ERROR
    assert entry.version == 3
    # The SSH transport's keys are gone rather than left beside the new ones.
    assert "ssh_user" not in entry.data
    assert "ssh_key" not in entry.data
    assert any(flow["context"]["source"] == "reauth"
               for flow in hass.config_entries.flow.async_progress())


async def test_adding_the_panel_again_is_refused_after_migration(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_app_probes
) -> None:
    """So the repair issue must NOT tell the operator to do that.

    The migrated entry still holds the panel's unique id, which is what keeps
    its device and history attached — so the re-add path is closed by design,
    not by accident. This test exists to keep the documented instruction and
    the closed path from drifting apart again.
    """
    from homeassistant import config_entries

    from .conftest import PASSWORD, PORT, mock_agent

    mock_agent(aioclient_mock)
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, title="KIOSK-PANEL-1",
        unique_id="kiosk-panel-1", data={"host": HOST, "ssh_user": "kiosk"},
    )
    entry.add_to_hass(hass)
    await setup_entry(hass, entry)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: HOST, CONF_PASSWORD: PASSWORD, CONF_PORT: PORT},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


def test_the_repair_issue_names_the_path_that_works() -> None:
    """A keyword check, and worth having despite that.

    It cannot verify prose, but it CAN stop the two specific sentences that
    have already been shipped wrong: an instruction to add the panel again,
    and one to delete the entry. Both name operations the tests above prove
    are closed or destructive. Read as a floor, not as proof the text is good.
    """
    import json
    import pathlib

    description = json.loads(
        (pathlib.Path(__file__).resolve().parents[1] / "custom_components"
         / "kiosk_pi" / "strings.json").read_text(encoding="utf-8")
    )["issues"]["agent_migration"]["description"].lower()

    assert "reconfigure" in description, (
        "the repair issue must name the reauth prompt, which is the only path "
        "that works after migration"
    )
    for closed in ("add the panel again under", "then remove the old entry"):
        assert closed not in description, (
            f"the repair issue still instructs {closed!r}, which "
            "test_adding_the_panel_again_is_refused_after_migration proves is "
            "closed"
        )
async def test_the_ssh_era_rows_are_adopted_so_no_entity_id_gains_a_2(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_app_probes
) -> None:
    """THE ENTITY ID IS THE CONTRACT AND THE REGISTRY NEVER FREES ONE.

    An ssh-era row keyed `<hostname>_resolution` holds
    `sensor.<host>_resolution` for ever. The 1.0.0 sensor is keyed on the entry
    id instead, so without this it registers beside the old row as
    `sensor.<host>_resolution_2`, and every dashboard and automation still
    pointing at the original reads a row nothing updates any more. Observed on
    the first panel, 2026-09-10, on `select.<host>_dashboard` and
    `sensor.<host>_resolution` both.
    """
    # 3.1 is where a panel lands after the transport migration and its reauth:
    # correct data, and the ssh-era registry rows still sitting beside it.
    entry = MockConfigEntry(
        domain=DOMAIN, version=3, minor_version=1,
        title="KIOSK-PANEL-1", unique_id="kiosk-panel-1",
        data={CONF_HOST: HOST, CONF_PASSWORD: PASSWORD, CONF_PORT: PORT,
              "hostname": "KIOSK-PANEL-1"},
    )
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    old_resolution = registry.async_get_or_create(
        "sensor", DOMAIN, "kiosk-panel-1_resolution",
        config_entry=entry, suggested_object_id="kiosk_panel_1_resolution",
    )
    old_dashboard = registry.async_get_or_create(
        "select", DOMAIN, "kiosk-panel-1_dashboard",
        config_entry=entry, suggested_object_id="kiosk_panel_1_dashboard",
    )

    mock_agent(aioclient_mock)
    assert await setup_entry(hass, entry)
    assert entry.minor_version == 3

    ids = {
        row.entity_id
        for row in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert not any(i.endswith("_2") for i in ids), (
        f"an ssh-era row was orphaned and its id taken by a _2 twin: "
        f"{sorted(i for i in ids if i.endswith('_2'))}"
    )
    # The SAME rows, re-keyed — so their recorded history is still theirs.
    assert old_resolution.entity_id in ids
    assert old_dashboard.entity_id in ids
    assert registry.async_get(old_resolution.entity_id).unique_id == (
        f"{entry.entry_id}_resolution"
    )


async def test_the_ssh_era_wifi_row_is_adopted_rather_than_abandoned(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_app_probes
) -> None:
    """The QUIET half of the shadowing problem, which has no `_2` to notice.

    `resolution` and `dashboard` collide: the ssh-era row holds the id the new
    entity wants, so the new one lands on `_2` and somebody eventually sees it.
    `wifi_signal` does not collide — 1.0.0 names the sensor "Wi-Fi signal"
    against the ssh era's "WiFi signal", so the slugs differ. Nothing collides,
    nothing looks wrong, and `sensor.<host>_wifi_signal` is simply abandoned
    while its replacement appears under a different id. Every dashboard and
    automation naming the old one breaks silently.

    Asserted on the unique_id after migration, because that is what decides
    which row the new entity picks up when the platform finally loads.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, title="KIOSK-PANEL-1",
        unique_id="kiosk-panel-1", data={"host": HOST, "ssh_user": "kiosk"},
    )
    entry.add_to_hass(hass)

    entities = er.async_get(hass)
    row = entities.async_get_or_create(
        "sensor", DOMAIN, "kiosk-panel-1_wifi_signal",
        config_entry=entry, suggested_object_id="kiosk_panel_1_wifi_signal",
        original_name="WiFi signal",
    )
    original_entity_id = row.entity_id

    await setup_entry(hass, entry)

    migrated = entities.async_get(original_entity_id)
    assert migrated is not None, "the ssh-era row was removed rather than re-keyed"
    assert migrated.unique_id == f"{entry.entry_id}_wifi_signal", (
        "the ssh-era wifi row still carries its old unique_id, so the 1.0.0 "
        "sensor will not adopt it — the id is abandoned and the replacement "
        "turns up as _wi_fi_signal with nothing to explain it"
    )


def test_every_mapped_key_is_a_real_1_0_0_entity_key() -> None:
    """A map entry naming a key no platform publishes can never be adopted.

    It would sit there looking like coverage while the ssh-era row stayed
    orphaned — the same silent shape the map exists to remove.
    """
    from custom_components.kiosk_pi import _SSH_ERA_UNIQUE_IDS
    from custom_components.kiosk_pi.binary_sensor import SENSORS as BINARY
    from custom_components.kiosk_pi.button import BUTTONS
    from custom_components.kiosk_pi.number import NUMBERS
    from custom_components.kiosk_pi.sensor import SENSORS
    from custom_components.kiosk_pi.switch import SWITCHES
    from custom_components.kiosk_pi.text import TEXTS

    published = {d.key for group in (BINARY, BUTTONS, NUMBERS, SENSORS,
                                     SWITCHES, TEXTS) for d in group}
    # Platforms that publish a single entity declare its key inline.
    published |= {"dashboard", "screenshot", "overlay_message", "agent"}

    unknown = sorted(set(_SSH_ERA_UNIQUE_IDS.values()) - published)
    assert not unknown, (
        f"the re-key map points at {unknown}, which no 1.0.0 platform "
        "publishes, so those rows would stay orphaned"
    )


async def test_a_v2_entry_reaches_the_row_adoption_step(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_app_probes
) -> None:
    """The v2 path must run EVERY migration step, not just the first.

    Setting minor_version straight to 2 in the 2->3 branch makes the 3.2 step
    a no-op for precisely the entries that need it: the ssh-era ones. It reads
    correct because an entry already at 3.1 — one panel in this fleet, migrated
    by an earlier build — does run the step, so a suite written around that
    entry passes while every remaining panel silently skips adoption.

    Asserted on the OUTCOME rather than on the minor_version, so renumbering
    the steps cannot make this pass vacuously.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, title="KIOSK-PANEL-9",
        unique_id="kiosk-panel-9", data={"host": HOST, "ssh_user": "kiosk"},
    )
    entry.add_to_hass(hass)

    entities = er.async_get(hass)
    for key in ("resolution", "dashboard", "wifi_signal"):
        entities.async_get_or_create(
            "sensor" if key != "dashboard" else "select",
            DOMAIN, f"kiosk-panel-9_{key}", config_entry=entry,
            suggested_object_id=f"kiosk_panel_9_{key}",
        )

    await setup_entry(hass, entry)

    stranded = [
        row.unique_id
        for row in er.async_entries_for_config_entry(entities, entry.entry_id)
        if row.unique_id.startswith("kiosk-panel-9_")
    ]
    assert not stranded, (
        f"a v2 entry skipped the row-adoption step; still ssh-keyed: {stranded}"
    )


async def test_adoption_survives_an_entry_the_operator_renamed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_app_probes
) -> None:
    """The ssh-era prefix must not be reconstructed from the entry title.

    `entry.data["hostname"]` is dropped by the 2->3 step and `entry.title` is
    editable in the UI, so an entry somebody renamed matched nothing and
    stranded every row in silence — no `_2`, no error, the same shape as the
    misses already fixed here.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, title="Office Wall",
        unique_id="kiosk-panel-1", data={"host": HOST, "ssh_user": "kiosk"},
    )
    entry.add_to_hass(hass)

    entities = er.async_get(hass)
    for key in ("resolution", "wifi_signal"):
        entities.async_get_or_create(
            "sensor", DOMAIN, f"kiosk-panel-1_{key}", config_entry=entry,
            suggested_object_id=f"kiosk_panel_1_{key}",
        )

    await setup_entry(hass, entry)

    stranded = [
        row.unique_id
        for row in er.async_entries_for_config_entry(entities, entry.entry_id)
        if not row.unique_id.startswith(f"{entry.entry_id}_")
    ]
    assert not stranded, (
        f"the entry was renamed, so the hostname could not be rebuilt and "
        f"these rows were stranded: {stranded}"
    )
