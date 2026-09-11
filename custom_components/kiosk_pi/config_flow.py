"""Config flow: add a panel, re-authenticate one, and set its options.

THE SETUP CHECK EXERCISES THE CHANNEL THE INTEGRATION WILL ACTUALLY USE. It
builds the same ``KioskPiClient``, over the same scheme, port and credential
that the coordinator will use, and calls a command on it. A flow that proved
reachability some other way — pinging the host, probing a different daemon —
certifies nothing, and it certifies nothing GREEN, which is worse than no check:
an entry created against a credential that does not work reads healthy on the
channel that was tested and goes dark on the one that is used.

It calls ``status`` rather than ``deviceInfo`` on purpose. At setup time the
question is "is this an agent, and is this password right", and ``deviceInfo``
also fails when the panel's browser is down — which would refuse a
correctly-configured wall that happens to be restarting.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from .api import (
    KioskPiAuthError,
    KioskPiClient,
    KioskPiConnectionError,
    KioskPiError,
)
from .const import (
    CONF_ALLOW_CONTROL_BOARDS,
    CONF_HOST,
    CONF_MAC,
    CONF_OFFLINE_EXPECTED,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_VERIFY_SSL,
    DEFAULT_PORT,
    DOMAIN,
    SETUP_TIMEOUT,
)
from .discovery import async_refresh_cache
from .external_apps import async_add_app, async_get_apps, async_remove_app
from .surface import SURFACES

_LOGGER = logging.getLogger(__name__)

STEP_USER = vol.Schema({
    vol.Required(CONF_HOST): str,
    vol.Required(CONF_PASSWORD): str,
    vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
    vol.Optional(CONF_SSL, default=False): bool,
    vol.Optional(CONF_VERIFY_SSL, default=True): bool,
})


class KioskPiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add one panel per agent."""

    # 3, not 1: versions 1 and 2 belong to the SSH transport, and an entry at
    # either of those is refused by async_migrate_entry rather than rewritten.
    # Reusing 1 here would make an old entry look current and let it load with
    # a configuration the client cannot read.
    VERSION = 3
    # 3.2 re-keys the ssh-era entity registry rows the 1.0.0 entities would
    # otherwise collide with, taking a `_2` id and orphaning the original.
    MINOR_VERSION = 3

    def __init__(self) -> None:
        self._discovered: dict[str, Any] = {}

    # --- manual -------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            info, error = await self._probe(user_input)
            if error:
                errors["base"] = error
            else:
                # UNIQUE ON THE PANEL'S OWN HOSTNAME, not on the address it was
                # typed at. A DHCP lease moving would otherwise let the same
                # panel be added twice, as two devices, splitting its history
                # between them with nothing to say they are one machine.
                unique = str(info.get("hostname") or user_input[CONF_HOST])
                existing = await self.async_set_unique_id(unique.lower())

                # ADOPT A PRE-1.0.0 ENTRY INSTEAD OF ABORTING ON IT.
                #
                # async_migrate_entry cannot carry a v2 entry across, and says
                # so: the agent's password is generated on the device by the
                # installer and does not exist at migration time. So the entry
                # sits in `migration_error` holding this unique id — and the
                # two-step upgrade that migration tells the operator to perform
                # ("install the agent, then add the panel again") was therefore
                # IMPOSSIBLE: _abort_if_unique_id_configured refused the second
                # step for as long as the first step's entry existed, and the
                # only way through was to delete that entry, which is exactly
                # what migration refused to do because it takes the panel's
                # recorded history with it.
                #
                # The credential that migration lacked is present HERE: the
                # operator just typed it, and _probe has already proven it
                # against the agent. So the function from old configuration to
                # new one exists at this point even though it does not exist at
                # migration time, and the entry is upgraded IN PLACE — same
                # entry_id, so the device and entity registry rows and every
                # recorded row behind them survive.
                if existing is not None and existing.version < self.VERSION:
                    _LOGGER.info(
                        "%s: upgrading the pre-1.0.0 entry in place to the "
                        "agent transport; its history is kept",
                        existing.title,
                    )
                    self.hass.config_entries.async_update_entry(
                        existing,
                        title=str(info.get("deviceName") or unique),
                        data={**user_input, **self._discovered},
                        options=existing.options,
                        version=self.VERSION,
                        minor_version=1,
                    )
                    ir.async_delete_issue(
                        self.hass, DOMAIN, f"agent_migration_{existing.entry_id}"
                    )
                    self.hass.config_entries.async_schedule_reload(
                        existing.entry_id
                    )
                    return self.async_abort(reason="upgraded")

                self._abort_if_unique_id_configured(
                    updates={CONF_HOST: user_input[CONF_HOST]}
                )
                return self.async_create_entry(
                    title=str(info.get("deviceName") or unique),
                    data={**user_input, **self._discovered},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER, {**(user_input or {}), **self._discovered}
            ),
            errors=errors,
        )

    async def _probe(
        self, config: dict[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        client = KioskPiClient(
            async_get_clientsession(self.hass),
            host=config[CONF_HOST],
            password=config[CONF_PASSWORD],
            port=config.get(CONF_PORT, DEFAULT_PORT),
            use_ssl=config.get(CONF_SSL, False),
            verify_ssl=config.get(CONF_VERIFY_SSL, True),
            timeout=SETUP_TIMEOUT,
        )
        try:
            await client.status()
        except KioskPiAuthError:
            return {}, "invalid_auth"
        except KioskPiConnectionError:
            return {}, "cannot_connect"
        except KioskPiError as err:
            _LOGGER.debug("unexpected agent reply during setup: %s", err)
            return {}, "unknown"
        # `status` is deliberately cheap and carries no identity, so the panel
        # is named from a second call. A failure here is not fatal: the entry
        # is created under the address and renames itself on the first refresh.
        try:
            return await client.device_info(), None
        except KioskPiError:
            return {}, None

    # --- discovery ----------------------------------------------------------

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """A panel seen on the network.

        DISCOVERY OFFERS, IT DOES NOT CONFIGURE. The agent's password is
        generated on the device by its installer and is not discoverable, so
        this can only pre-fill the address and hand the operator a form. A flow
        that tried a default password here would either fail for everybody or,
        far worse, succeed — which would mean the fleet shipped with one.
        """
        await self.async_set_unique_id(discovery_info.hostname.lower())
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: discovery_info.ip}
        )
        self._discovered = {
            CONF_HOST: discovery_info.ip,
            CONF_MAC: discovery_info.macaddress,
        }
        self.context["title_placeholders"] = {"name": discovery_info.hostname}
        return await self.async_step_user()

    # --- reauth -------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-enter the password after the agent refused it.

        Re-provisioning a panel regenerates its password, so this is a routine
        step rather than an incident — which is why the coordinator raises
        ConfigEntryAuthFailed on a 401 instead of retrying a credential that
        will still be wrong in thirty seconds.
        """
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            candidate = {**entry.data, **user_input}
            _info, error = await self._probe(candidate)
            if error:
                errors["base"] = error
            else:
                # The upgrade is finished, so the repair telling the operator
                # to finish it is now noise on their dashboard.
                ir.async_delete_issue(
                    self.hass, DOMAIN, f"agent_migration_{entry.entry_id}"
                )
                return self.async_update_reload_and_abort(entry, data=candidate)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"host": entry.data[CONF_HOST]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> KioskPiOptionsFlow:
        return KioskPiOptionsFlow()


class KioskPiOptionsFlow(OptionsFlow):
    """Per-panel options, plus the shared list of dashboard apps."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["panel", "add_app", "remove_app"],
        )

    async def async_step_panel(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            # MERGED OVER THE EXISTING OPTIONS, NEVER REPLACING THEM.
            # `async_create_entry(data=...)` REPLACES `entry.options` wholesale,
            # so a step returning only its own keys deletes every other step's
            # — silently, with no edit to point at. Harmless while a flow has
            # one step, which is how it survives to the commit that adds a
            # second; this flow has three.
            return self.async_create_entry(
                data={**self.config_entry.options, **user_input}
            )
        options = self.config_entry.options
        schema = vol.Schema({
            vol.Optional(
                CONF_ALLOW_CONTROL_BOARDS,
                default=options.get(CONF_ALLOW_CONTROL_BOARDS, False),
            ): bool,
            vol.Optional(
                CONF_OFFLINE_EXPECTED,
                default=options.get(CONF_OFFLINE_EXPECTED, False),
            ): bool,
        })
        return self.async_show_form(step_id="panel", data_schema=schema)

    async def async_step_add_app(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            await async_add_app(
                self.hass,
                key=user_input["key"],
                url=user_input["url"],
                surface=user_input["surface"],
            )
            await async_refresh_cache(self.hass)
            return self.async_create_entry(data={**self.config_entry.options})
        schema = vol.Schema({
            vol.Required("key"): str,
            vol.Required("url"): str,
            vol.Required("surface", default="monitor"): vol.In(list(SURFACES)),
        })
        return self.async_show_form(step_id="add_app", data_schema=schema)

    async def async_step_remove_app(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        apps = await async_get_apps(self.hass)
        if not apps:
            return self.async_abort(reason="no_apps")
        if user_input is not None:
            await async_remove_app(self.hass, user_input["key"])
            await async_refresh_cache(self.hass)
            return self.async_create_entry(data={**self.config_entry.options})
        schema = vol.Schema({
            vol.Required("key"): vol.In(sorted(str(app["key"]) for app in apps)),
        })
        return self.async_show_form(step_id="remove_app", data_schema=schema)
