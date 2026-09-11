"""The HTTP client for one ``pikioskd`` agent.

IMPORTS NOTHING FROM ``homeassistant``, deliberately, so the transport can be
exercised without a Home Assistant, which is what makes the transport testable
by inspection. It takes an ``aiohttp`` session because Home Assistant
supplies one; it does not create one, because a client that owns its own
session leaks a connector per config entry reload.

THREE FAILURE MODES, KEPT APART, because they send an operator to three
different places:

  ``KioskPiConnectionError``  the agent could not be reached at all — wrong
                              address, wrong port, host down, agent not running.
  ``KioskPiAuthError``        it was reached and refused the password.
  ``KioskPiCommandError``     it was reached, authorised the caller, and the
                              command failed on the device (usually because the
                              browser is down, which the agent reports as 502).

Collapsing the third into the first is the expensive one: a panel whose browser
has crashed but whose agent is answering perfectly would read as an unreachable
host, and the operator would go and check the network.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from yarl import URL

_LOGGER = logging.getLogger(__name__)


class KioskPiError(Exception):
    """Base for every failure this client raises."""


class KioskPiConnectionError(KioskPiError):
    """The agent could not be reached."""


class KioskPiAuthError(KioskPiError):
    """The agent refused the password, or remote administration is off."""


class KioskPiCommandError(KioskPiError):
    """The agent was reached and the command failed on the device."""


class KioskPiClient:
    """One agent, addressed by host and port."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        password: str,
        port: int,
        *,
        use_ssl: bool = False,
        verify_ssl: bool = True,
        timeout: int = 15,
    ) -> None:
        self._session = session
        self._host = host
        self._password = password
        self._port = port
        self._scheme = "https" if use_ssl else "http"
        self._verify_ssl = verify_ssl
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    @property
    def host(self) -> str:
        return self._host

    @property
    def base_url(self) -> str:
        return f"{self._scheme}://{self._host}:{self._port}"

    def _url(self, command: str, params: dict[str, Any] | None = None) -> URL:
        """Build the request URL.

        THE PASSWORD IS NOT PUT HERE. It goes in the Authorization header, so
        it cannot end up in an aiohttp debug log, an exception's ``str()``, or
        a diagnostics dump — all of which print the URL. The agent accepts both
        forms precisely so a programmatic caller can use the header one.
        """
        query: dict[str, str] = {"cmd": command}
        for key, value in (params or {}).items():
            if value is None:
                continue
            if isinstance(value, bool):
                query[key] = "true" if value else "false"
            else:
                query[key] = str(value)
        return URL(self.base_url).with_query(query)

    async def _request(
        self, command: str, params: dict[str, Any] | None = None,
        *, raw: bool = False,
    ) -> Any:
        url = self._url(command, params)
        headers = {"Authorization": f"Bearer {self._password}"}
        try:
            response = await self._session.get(
                url, headers=headers, timeout=self._timeout,
                ssl=None if not self._verify_ssl else True,
            )
        except asyncio.TimeoutError as err:
            raise KioskPiConnectionError(
                f"{self._host}: timed out after {self._timeout.total}s"
            ) from err
        except aiohttp.ClientError as err:
            raise KioskPiConnectionError(f"{self._host}: {err}") from err

        async with response:
            if response.status in (401, 403):
                # Read the body for the reason — the agent distinguishes a bad
                # password (401) from remote administration being switched off
                # (403), and an operator who is told only "unauthorised" will
                # go and re-type a password that was already correct.
                detail = await self._error_text(response)
                raise KioskPiAuthError(f"{self._host}: {detail}")

            if raw and response.status == 200:
                return await response.read()

            # PARSE IT, DO NOT INTERROGATE THE HEADER. A Content-Type equality
            # check refuses a perfectly good reply whenever the header is
            # absent, or carries a charset, or has been rewritten by a reverse
            # proxy in front of the panel — and it refuses it as a COMMAND
            # failure, which points the operator at the device rather than at
            # the middlebox. Parsing answers the real question: is this an
            # agent. A captive portal's login page and a router's 404 both fail
            # to parse and are reported with their body, which is what actually
            # identifies them.
            try:
                payload = await response.json(content_type=None)
            except (aiohttp.ClientError, ValueError) as err:
                body = (await response.text())[:200]
                raise KioskPiCommandError(
                    f"{self._host}: {command} answered {response.status} "
                    f"with something that is not JSON ({err}): {body!r}"
                ) from err
            if not isinstance(payload, dict):
                raise KioskPiCommandError(
                    f"{self._host}: {command} answered {response.status} with "
                    f"a JSON {type(payload).__name__}, not an object"
                )
            if response.status != 200 or payload.get("status") != "OK":
                raise KioskPiCommandError(
                    f"{self._host}: {command} failed "
                    f"({response.status}): {payload.get('statustext', payload)}"
                )
            return payload

    @staticmethod
    async def _error_text(response: aiohttp.ClientResponse) -> str:
        try:
            payload = await response.json()
        except (aiohttp.ClientError, ValueError):
            return f"HTTP {response.status}"
        return str(payload.get("statustext") or f"HTTP {response.status}")

    # --- reads --------------------------------------------------------------

    async def device_info(self) -> dict[str, Any]:
        return await self._request("deviceInfo")

    async def list_settings(self) -> dict[str, Any]:
        return await self._request("listSettings")

    async def status(self) -> dict[str, Any]:
        """The cheap liveness read, touching no hardware on the device.

        Used by the config flow rather than ``deviceInfo``: at setup time the
        question is whether this address is an agent and whether the password
        is right, and answering it with a call that also fails when the browser
        is down would refuse a correctly-configured panel whose wall happens to
        be restarting.
        """
        return await self._request("status")

    async def screenshot(self) -> bytes:
        return await self._request("getScreenshot", raw=True)

    # --- writes -------------------------------------------------------------

    async def load_url(self, url: str) -> dict[str, Any]:
        return await self._request("loadURL", {"url": url})

    async def load_start_url(self) -> dict[str, Any]:
        return await self._request("loadStartURL")

    async def restart_browser(self) -> dict[str, Any]:
        return await self._request("restartApp")

    async def reboot(self) -> dict[str, Any]:
        return await self._request("rebootDevice")

    async def clear_cache(self) -> dict[str, Any]:
        return await self._request("clearCache")

    async def clear_cookies(self) -> dict[str, Any]:
        return await self._request("clearCookies")

    async def to_foreground(self) -> dict[str, Any]:
        return await self._request("toForeground")

    async def set_overlay(self, text: str) -> dict[str, Any]:
        return await self._request("setOverlayMessage", {"text": text})

    async def screen_on(self) -> dict[str, Any]:
        return await self._request("screenOn")

    async def screen_off(self) -> dict[str, Any]:
        return await self._request("screenOff")

    async def set_brightness(self, level: int) -> dict[str, Any]:
        return await self._request("setBrightness", {"level": level})

    async def start_screensaver(self) -> dict[str, Any]:
        return await self._request("startScreensaver")

    async def stop_screensaver(self) -> dict[str, Any]:
        return await self._request("stopScreensaver")

    async def set_string_setting(self, key: str, value: str) -> dict[str, Any]:
        return await self._request("setStringSetting", {"key": key, "value": value})

    async def set_bool_setting(self, key: str, value: bool) -> dict[str, Any]:
        return await self._request("setBooleanSetting", {"key": key, "value": value})

    async def set_int_setting(self, key: str, value: int) -> dict[str, Any]:
        return await self._request("setIntSetting", {"key": key, "value": value})
