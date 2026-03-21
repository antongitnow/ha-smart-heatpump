"""Persistent storage for thermal observations.

Uses Home Assistant's Store helper to save/load observations as JSON
in the .storage/ directory. Survives HA restarts.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .thermal_model import (
    ThermalObservation,
    compute_loss_coefficient,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = "smart_heatpump_thermal"

# Keep 7 days of data at 15-minute intervals
MAX_OBSERVATIONS = 672


class ThermalStore:
    """Manages thermal observations and the learned loss coefficient."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY}_{entry_id}"
        )
        self.observations: list[ThermalObservation] = []
        self.loss_coefficient: float | None = None

    async def async_load(self) -> None:
        """Load observations from persistent storage."""
        data: dict[str, Any] | None = await self._store.async_load()
        if not data:
            return

        raw_obs = data.get("observations", [])
        self.observations = [
            ThermalObservation(
                timestamp=o["ts"],
                indoor_temp_c=o["indoor"],
                outdoor_temp_c=o["outdoor"],
                heating_active=o.get("heating", False),
            )
            for o in raw_obs
            if isinstance(o, dict)
        ]

        self.loss_coefficient = data.get("loss_coefficient")
        _LOGGER.info(
            "Loaded %d thermal observations, k=%s",
            len(self.observations),
            self.loss_coefficient,
        )

    async def async_save(self) -> None:
        """Persist observations to storage."""
        data = {
            "observations": [
                {
                    "ts": o.timestamp,
                    "indoor": o.indoor_temp_c,
                    "outdoor": o.outdoor_temp_c,
                    "heating": o.heating_active,
                }
                for o in self.observations
            ],
            "loss_coefficient": self.loss_coefficient,
        }
        await self._store.async_save(data)

    def add_observation(
        self,
        timestamp: float,
        indoor_temp_c: float,
        outdoor_temp_c: float,
        heating_active: bool = False,
    ) -> None:
        """Record an observation and recompute the loss coefficient."""
        self.observations.append(
            ThermalObservation(
                timestamp=timestamp,
                indoor_temp_c=indoor_temp_c,
                outdoor_temp_c=outdoor_temp_c,
                heating_active=heating_active,
            )
        )

        # Trim to rolling window
        if len(self.observations) > MAX_OBSERVATIONS:
            self.observations = self.observations[-MAX_OBSERVATIONS:]

        # Recompute loss coefficient
        new_k = compute_loss_coefficient(self.observations)
        if new_k is not None:
            self.loss_coefficient = new_k
            _LOGGER.debug("Updated loss coefficient: k=%.5f", new_k)

    @property
    def sample_count(self) -> int:
        """Number of observations collected."""
        return len(self.observations)

    @property
    def is_ready(self) -> bool:
        """Whether the model has enough data to make predictions."""
        return self.loss_coefficient is not None
