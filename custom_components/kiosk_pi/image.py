"""The screenshot: what the wall is actually painting, as a picture.

FETCHED ON DEMAND, NOT ON THE POLL. A PNG of a 2560x1440 panel is megabytes,
and pulling one every thirty seconds from four panels would be the integration's
entire cost, for an image nobody is looking at most of the time. The image
platform asks only when something renders it.

``image_last_updated`` MOVES ON EVERY COORDINATOR REFRESH, which is what makes
the frontend re-fetch. It deliberately does NOT track the moment the PNG was
captured — the entity has no way to know that without capturing one, which is
the cost this design exists to avoid.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import KioskPiConfigEntry
from .api import KioskPiError
from .coordinator import KioskPiCoordinator
from .entity import KioskPiBrowserEntity

# READ-ONLY PLATFORM. Every value comes off the coordinator's single poll, so
# there is nothing here for Home Assistant to serialise and a limit would only
# slow entity addition down.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KioskPiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([KioskPiScreenshot(hass, entry.runtime_data)])


class KioskPiScreenshot(KioskPiBrowserEntity, ImageEntity):
    _attr_translation_key = "screenshot"
    _attr_content_type = "image/png"

    def __init__(self, hass: HomeAssistant,
                 coordinator: KioskPiCoordinator) -> None:
        KioskPiBrowserEntity.__init__(self, coordinator, "screenshot")
        ImageEntity.__init__(self, hass)
        self._cached: bytes | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        # Invalidate rather than re-fetch. The next render pulls a fresh PNG;
        # until then nothing crosses the network.
        self._cached = None
        self._attr_image_last_updated = dt_util.utcnow()
        super()._handle_coordinator_update()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._attr_image_last_updated is None:
            self._attr_image_last_updated = dt_util.utcnow()

    async def async_image(self) -> bytes | None:
        if self._cached is not None:
            return self._cached
        try:
            self._cached = await self.coordinator.client.screenshot()
        except KioskPiError:
            # A screenshot that could not be taken is no image, not a stale
            # one. Serving the previous frame would show an operator a board
            # that is no longer on the glass, which is worse than a blank.
            return None
        return self._cached

    @property
    def image_last_updated(self) -> datetime | None:
        return self._attr_image_last_updated
