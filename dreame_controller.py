"""
dreame_controller.py — Dreame wrapper using Tasshack/dreame-vacuum beta protocol.

Handles:
  - Dreame cloud authentication
  - Device and room discovery
  - Pre-clean status checks (battery, state, errors)
  - Segment cleaning and stop/dock commands
"""

import asyncio
import logging
from typing import Dict, Any, List

from dreame.device import DreameVacuumDevice
from dreame.types import DreameVacuumCleaningMode
from dreame.protocol import DreameVacuumProtocol

log = logging.getLogger(__name__)

# Result strings returned by get_status()
STATUS_OK = "ok"
STATUS_ALREADY_CLEANING = "already_cleaning"
STATUS_LOW_BATTERY = "low_battery"
STATUS_ERROR = "error"

# State names that count as actively cleaning.
_CLEANING_STATE_NAMES = {
    "CLEANING",
    "ZONE_CLEANING",
    "SEGMENT_CLEANING",
    "SPOT_CLEANING",
    "CUSTOM_CLEANING",
    "PART_CLEANING",
    "SWEEPING",
    "SWEEPING_AND_MOPPING",
}

class DreameController:
    """Async wrapper around DreameVacuumDevice from Tasshack."""

    def __init__(self, min_battery_percent: int = 30) -> None:
        self.min_battery_percent = min_battery_percent
        self._username: str | None = None
        self._password: str | None = None
        self._country: str | None = None
        self._account_type: str = "mi"
        self._device: DreameVacuumDevice | None = None
        self._host: str | None = None
        self._token: str | None = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def setup(
        self,
        username: str,
        password: str = None,
        country: str = "cn",
        account_type: str = "mi",
        cached_credentials: Dict = None,
        interactive: bool = True,
    ) -> Dict:
        """
        Authenticate with the Dreame/Xiaomi cloud.
        """
        self._username = username
        self._country = country
        self._account_type = account_type or "mi"

        if cached_credentials:
            if not self._password:
                self._password = cached_credentials.get("password")
            self._host = cached_credentials.get("host")
            self._token = cached_credentials.get("token")
            log.info("Restored Dreame session from cache")
            if self._password and self._host and self._token:
                return cached_credentials

        if not interactive and not self._password:
            raise RuntimeError(
                "No valid cached credentials and interactive=False.\n"
                "Run first:  python mamad_roborock.py --setup"
            )

        if not self._password:
            import getpass
            self._password = getpass.getpass(f"Enter password for Xiaomi account {username}: ")

        log.info("Login info provided for %s. (Device discovery will be performed next.)", username)

        return {
            "username": self._username,
            "password": self._password,
            "country": self._country,
            "account_type": self._account_type,
            "host": self._host,
            "token": self._token
        }

    # ------------------------------------------------------------------
    # Device & room discovery
    # ------------------------------------------------------------------

    async def discover_devices(self) -> None:
        """Create the device and connect to it."""
        if not self._username or not self._password:
            raise RuntimeError("Call setup() before discover_devices()")

        protocol = DreameVacuumProtocol(
            username=self._username,
            password=self._password,
            country=self._country,
            account_type=self._account_type,
            prefer_cloud=True,
        )
        cloud = protocol.cloud
        if cloud is None:
            raise RuntimeError("Dreame cloud protocol unavailable")

        try:
            log.info("Logging into cloud (country=%s, account_type=%s)...", self._country, self._account_type)
            logged_in = await asyncio.get_event_loop().run_in_executor(None, cloud.login)
            if not logged_in:
                raise RuntimeError(
                    f"Dreame cloud login failed (country={self._country}, account_type={self._account_type})."
                )
            log.info("Fetching devices...")
            devices = await asyncio.get_event_loop().run_in_executor(None, cloud.get_devices)
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch devices from cloud: {exc}")

        device_list = []
        if isinstance(devices, list):
            device_list = [d for d in devices if isinstance(d, dict)]
        elif isinstance(devices, dict):
            page = devices.get("page")
            if isinstance(page, dict) and isinstance(page.get("records"), list):
                device_list = [d for d in page["records"] if isinstance(d, dict)]
            elif isinstance(devices.get("list"), list):
                device_list = [d for d in devices["list"] if isinstance(d, dict)]

        if not device_list:
            raise RuntimeError("No Dreame devices found on this account")

        device_info = device_list[0]
        did = device_info.get("did")
        mac = device_info.get("mac") or device_info.get("MAC")
        self._host = device_info.get("localip")
        self._token = device_info.get("token")

        if not self._host:
            if mac:
                token, host = await asyncio.get_event_loop().run_in_executor(None, cloud.get_info, str(mac))
                self._host = host
                self._token = token
            elif did:
                cloud._did = str(did)
                info = await asyncio.get_event_loop().run_in_executor(None, cloud.get_device_info)
                if isinstance(info, dict):
                    self._host = info.get("host")
                # Dreame account cloud path does not require local 32-char token.
                self._token = self._token or " "

        name = device_info.get("name", "Unknown Dreame Vacuum")
        mac = mac or ""

        if not self._host:
            raise RuntimeError("Failed to resolve device host from cloud")

        log.info("Using device: %s (host=%s)", name, self._host)

        self._device = DreameVacuumDevice(
            name=name,
            host=self._host,
            token=self._token or " ",
            mac=mac,
            username=self._username,
            password=self._password,
            country=self._country,
            prefer_cloud=True,
            account_type=self._account_type,
            device_id=did,
        )

        try:
            log.info("Connecting to device and updating properties...")
            await asyncio.get_event_loop().run_in_executor(None, self._device.update)
            map_manager = getattr(self._device, "map_manager", None)
            if map_manager:
                await asyncio.get_event_loop().run_in_executor(None, map_manager.update)
        except Exception as exc:
            log.warning("Initial device connection error: %s", exc)

    async def discover_rooms(self) -> List[Dict]:
        """
        Return a list of ``{"id": int, "name": str}`` dicts for all rooms.
        """
        if self._device is None:
            raise RuntimeError("Call discover_devices() before discover_rooms()")

        current_map = self._device.status.current_map
        if not current_map:
            log.warning("No map found on device. Returning empty room list.")
            return []

        rooms = []
        try:
            # Map elements
            for seg_id, segment in (current_map.segments or {}).items():
                rooms.append({"id": seg_id, "name": segment.name or f"Room {seg_id}"})

            log.info(
                "Discovered %d rooms: %s",
                len(rooms),
                [(r["id"], r["name"]) for r in rooms],
            )
        except Exception as exc:
            log.warning("Room discovery failed: %s", exc)

        return rooms

    # ------------------------------------------------------------------
    # Status & commands
    # ------------------------------------------------------------------

    async def get_status(self) -> Dict[str, Any]:
        """
        Refresh and return vacuum status.

        Returns dict with keys: state (str), battery (int), error_code (int), result (str).
        """
        if self._device is None:
            raise RuntimeError("Call discover_devices() first")

        await asyncio.get_event_loop().run_in_executor(None, self._device.update)

        state = self._device.status.state
        battery = self._device.status.battery_level
        has_error = self._device.status.has_error

        state_name = state.name if state else "unknown"
        log.debug(
            "Vacuum status: state=%s battery=%d error=%s",
            state_name, battery, has_error
        )

        if has_error:
            result = STATUS_ERROR
        elif state_name in _CLEANING_STATE_NAMES:
            result = STATUS_ALREADY_CLEANING
        elif battery < self.min_battery_percent:
            result = STATUS_LOW_BATTERY
        else:
            result = STATUS_OK

        return {
            "state": state_name,
            "battery": battery,
            "error_code": 1 if has_error else 0,
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
        log.info("Starting segment clean: segment_id=%d profile=%s", segment_id, profile)

        mode_map = {
            "vacuum_only": DreameVacuumCleaningMode.SWEEPING.value,
            "mop_only": DreameVacuumCleaningMode.MOPPING.value,
            "vacuum_and_mop": DreameVacuumCleaningMode.SWEEPING_AND_MOPPING.value,
            "mop_after_vacuum": DreameVacuumCleaningMode.MOPPING_AFTER_SWEEPING.value,
        }

        if profile in mode_map:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    self._device.set_cleaning_mode,
                    mode_map[profile],
                )
            except Exception as exc:
                raise RuntimeError(f"Failed to apply cleaning profile '{profile}': {exc}") from exc
        elif profile != "auto":
            log.warning("Unknown cleaning profile '%s' — using current robot mode", profile)

        # Mapping from string to the integer values if applicable
        # The Tasshack library usually accepts ints, but we can pass suction_level mapped
        # Assuming we just trigger clean_segment and let it use defaults
        await asyncio.get_event_loop().run_in_executor(
            None,
            self._device.clean_segment,
            [segment_id],
            1, # cleaning_times
            None, # suction_level
            None, # water_volume
        )

    async def stop_and_dock(self) -> None:
        """Stop the current job and send the vacuum home."""
        if self._device is None:
            return

        log.info("Stopping and returning to dock")
        try:
            await asyncio.get_event_loop().run_in_executor(None, self._device.stop)
            await asyncio.sleep(2)
            await asyncio.get_event_loop().run_in_executor(None, self._device.return_to_base)
        except Exception as exc:
            log.warning("stop_and_dock error: %s", exc)

    async def close(self) -> None:
        """Close connections."""
        if self._device is not None:
            self._device.disconnect()
