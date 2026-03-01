"""
vacuum_controller.py — python-roborock v4.x wrapper.

Handles:
  - Roborock cloud authentication (email code login with UserData caching)
  - Device and room discovery
  - Pre-clean status checks (battery, state, errors)
  - Segment cleaning and stop/dock commands
"""

import logging
from typing import Dict, List, Any, Optional

from roborock.data.containers import UserData
from roborock.devices.device_manager import UserParams, create_device_manager
from roborock.roborock_typing import RoborockCommand
from roborock.web_api import RoborockApiClient

log = logging.getLogger(__name__)

# Result strings returned by get_status()
STATUS_OK = "ok"
STATUS_ALREADY_CLEANING = "already_cleaning"
STATUS_LOW_BATTERY = "low_battery"
STATUS_ERROR = "error"

# State code values that count as "actively cleaning"
_CLEANING_STATE_VALUES = {
    5,   # cleaning
    11,  # spot_cleaning
    17,  # zoned_cleaning
    18,  # segment_cleaning
    29,  # mapping
    6301, 6302, 6303, 6304, 6305, 6306, 6307, 6308, 6309,  # mop variants
}

# Fan speed name → Roborock integer (device-dependent, S/Q-series)
_FAN_SPEED_MAP = {
    "quiet": 101,
    "balanced": 102,
    "turbo": 103,
    "max": 104,
    "max_plus": 105,
}


class VacuumController:
    """Async wrapper around python-roborock v4.x."""

    def __init__(self, min_battery_percent: int = 30) -> None:
        self.min_battery_percent = min_battery_percent
        self._email: str | None = None
        self._user_data: UserData | None = None
        self._device_manager = None
        self._device = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def setup(
        self,
        email: str,
        cached_credentials: Dict = None,
        interactive: bool = True,
    ) -> Dict:
        """
        Authenticate with the Roborock cloud.

        If *cached_credentials* is non-empty, restores UserData from cache
        without prompting. Otherwise does the email code flow.

        Returns a serialisable credentials dict for caching in state file.
        """
        self._email = email

        if cached_credentials:
            try:
                self._user_data = UserData.from_dict(cached_credentials)
                if self._user_data is not None:
                    log.info("Restored Roborock session from cache")
                    return cached_credentials
            except Exception as exc:
                log.warning("Cached credentials invalid (%s) — re-authenticating", exc)

        # Email code login
        client = RoborockApiClient(email)
        log.info("Requesting Roborock login code for %s", email)
        await client.request_code()

        if not interactive:
            raise RuntimeError(
                "No valid cached credentials and interactive=False.\n"
                "Run first:  python mamad_roborock.py --setup"
            )

        code = input(f"Enter the verification code sent to {email}: ").strip()
        self._user_data = await client.code_login(code)
        log.info("Login successful")
        return self._user_data.as_dict()

    # ------------------------------------------------------------------
    # Device & room discovery
    # ------------------------------------------------------------------

    async def discover_devices(self) -> None:
        """Create the device manager and store the first vacuum found."""
        if self._user_data is None or self._email is None:
            raise RuntimeError("Call setup() before discover_devices()")

        user_params = UserParams(username=self._email, user_data=self._user_data)
        self._device_manager = await create_device_manager(user_params)

        devices = await self._device_manager.get_devices()
        if not devices:
            raise RuntimeError("No Roborock devices found on this account")

        self._device = devices[0]
        log.info(
            "Using device: %s (duid=%s)",
            getattr(self._device, "name", "unknown"),
            getattr(self._device, "duid", "?"),
        )

    async def discover_rooms(self) -> List[Dict]:
        """
        Return a list of ``{"id": int, "name": str}`` dicts for all rooms.

        Refreshes the rooms trait from the device, then reads room_map.
        Falls back to an empty list on any failure.
        """
        if self._device is None:
            raise RuntimeError("Call discover_devices() before discover_rooms()")

        try:
            rooms_trait = self._device.v1_properties.rooms
            await rooms_trait.refresh()
            room_map = rooms_trait.room_map  # dict[int, NamedRoomMapping]
            rooms = [
                {"id": mapping.segment_id, "name": mapping.name}
                for mapping in room_map.values()
            ]
            log.info(
                "Discovered %d rooms: %s",
                len(rooms),
                [(r["id"], r["name"]) for r in rooms],
            )
            return rooms
        except Exception as exc:
            log.warning("Room discovery failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Status & commands
    # ------------------------------------------------------------------

    async def get_status(self) -> Dict[str, Any]:
        """
        Refresh and return vacuum status.

        Returns dict with keys: state (str), battery (int), error_code (int), result (str).
        result is one of: STATUS_OK / STATUS_ALREADY_CLEANING / STATUS_LOW_BATTERY / STATUS_ERROR
        """
        if self._device is None:
            raise RuntimeError("Call discover_devices() first")

        status_trait = self._device.v1_properties.status
        await status_trait.refresh()

        state_code = status_trait.state       # RoborockStateCode enum or None
        battery = status_trait.battery or 100
        error_code = status_trait.error_code  # RoborockErrorCode enum or None

        state_name = state_code.name if state_code is not None else "unknown"
        state_value = state_code.value if state_code is not None else 0
        error_value = error_code.value if error_code is not None else 0

        log.debug(
            "Vacuum status: state=%s(%d) battery=%d error=%s(%d)",
            state_name, state_value, battery, error_code, error_value,
        )

        if state_value == 12 or error_value != 0:  # error state or error code set
            result = STATUS_ERROR
        elif state_value in _CLEANING_STATE_VALUES:
            result = STATUS_ALREADY_CLEANING
        elif battery < self.min_battery_percent:
            result = STATUS_LOW_BATTERY
        else:
            result = STATUS_OK

        return {
            "state": state_name,
            "battery": battery,
            "error_code": error_value,
            "result": result,
        }

    async def start_segment_clean(
        self,
        segment_id: int,
        fan_speed: str = "balanced",
        cleaning_profile: str = "auto",
    ) -> None:
        """Start cleaning a single segment (room)."""
        if self._device is None:
            raise RuntimeError("Call discover_devices() first")

        profile = (cleaning_profile or "auto").strip().lower()
        if profile != "auto":
            log.info(
                "Requested cleaning_profile=%s on Roborock; using device defaults with segment clean parameters",
                profile,
            )

        fan_speed_val = _FAN_SPEED_MAP.get(fan_speed.lower(), _FAN_SPEED_MAP["balanced"])
        params = [{"segments": [segment_id], "repeat": 1, "fan_speed": fan_speed_val}]

        log.info(
            "Starting segment clean: segment_id=%d fan_speed=%s(%d)",
            segment_id, fan_speed, fan_speed_val,
        )
        await self._device.v1_properties.command.send(
            RoborockCommand.APP_SEGMENT_CLEAN, params=params
        )

    async def stop_and_dock(self) -> None:
        """Stop the current job and send the vacuum home."""
        if self._device is None:
            return

        import asyncio
        log.info("Stopping and returning to dock")
        try:
            await self._device.v1_properties.command.send(RoborockCommand.APP_STOP)
        except Exception as exc:
            log.debug("APP_STOP error (may already be stopped): %s", exc)

        # Give the device a moment to process the stop before issuing charge
        await asyncio.sleep(2)
        await self._device.v1_properties.command.send(RoborockCommand.APP_CHARGE)

    async def close(self) -> None:
        """Close MQTT connections cleanly."""
        if self._device_manager is not None:
            try:
                await self._device_manager.close()
            except Exception as exc:
                log.debug("Error closing device manager: %s", exc)
