#!/usr/bin/env python3
"""
mamad_roborock.py — MAMAD Roborock Missile Alert Cleaner

Entry point and async orchestrator.

Usage:
    python mamad_roborock.py [--setup] [--config config.yaml]

Modes:
    --setup   Interactive first-run: authenticates, discovers rooms, prints them.
    (default) Daemon loop: monitors alerts and triggers cleaning.
"""

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import signal
import sys
from typing import Dict, Any, List, Optional

import aiohttp
import yaml

from alert_monitor import (
    AlertMonitor,
    fetch_known_areas,
    validate_configured_areas,
    TEST_CITY_TOKEN,
    _cache_bust_url,
    _decode_response,
)
from notifications import Notifier
from room_scheduler import RoomScheduler
from vacuum_controller import (
    VacuumController,
    STATUS_OK,
    STATUS_ALREADY_CLEANING,
)
from dreame_controller import DreameController

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging(cfg: Dict[str, Any]) -> None:
    level = getattr(logging, cfg.get("log_level", "INFO").upper(), logging.INFO)
    log_file = cfg.get("log_file", "mamad.log")
    max_bytes = cfg.get("log_max_bytes", 5 * 1024 * 1024)
    backup_count = cfg.get("log_backup_count", 3)

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if log_file:
        os.makedirs(os.path.dirname(log_file) if os.path.dirname(log_file) else ".", exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        fh.setFormatter(logging.Formatter(fmt))
        handlers.append(fh)

    logging.basicConfig(level=level, format=fmt, handlers=handlers)


log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_config(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except FileNotFoundError:
        sys.exit(f"ERROR: Config file not found: {path}\n"
                 "Copy config.yaml to the working directory and fill in your settings.")
    except yaml.YAMLError as exc:
        sys.exit(f"ERROR: Config file parse error: {exc}")

    if not isinstance(cfg, dict):
        sys.exit("ERROR: Config file must be a YAML mapping (dict)")

    return cfg


# ---------------------------------------------------------------------------
# Interactive helpers
# ---------------------------------------------------------------------------

_FILTER_MAX_SHOWN = 8

_CLEANING_PROFILES = {
    "auto": "Keep robot/app default",
    "vacuum_only": "Vacuum only (no mopping)",
    "mop_only": "Mop only",
    "vacuum_and_mop": "Vacuum + mop together",
    "mop_after_vacuum": "Mop after vacuum",
}


def _prompt_with_default(prompt: str, default: str) -> str:
    raw = input(f"{prompt} [{default}]: ").strip()
    return raw or default


def _normalize_cleaning_profile(value: str) -> str:
    profile = (value or "auto").strip().lower()
    aliases = {
        "1": "auto",
        "2": "vacuum_only",
        "3": "mop_only",
        "4": "vacuum_and_mop",
        "5": "mop_after_vacuum",
    }
    profile = aliases.get(profile, profile)
    if profile not in _CLEANING_PROFILES:
        return "auto"
    return profile


def _prompt_cleaning_profile(default: str) -> str:
    print("\n--- Cleaning Behavior ---")
    print("Choose what to do when an alert triggers cleaning:")
    print("  1. Keep robot/app default")
    print("  2. Vacuum only (no mopping)")
    print("  3. Mop only")
    print("  4. Vacuum + mop together")
    print("  5. Mop after vacuum")
    value = _prompt_with_default("Select cleaning behavior (1-5 or name)", default)
    return _normalize_cleaning_profile(value)


def _prompt_areas(existing: List[str] = None, known_areas: List[str] = None) -> List[str]:
    """
    Interactively prompt for alert areas using a type-to-filter + pick-by-number loop.

    The user types any substring to filter the city list, then enters a number to
    select from the shown matches.  An empty line finishes entry.  Works with plain
    input() — no special terminal control or extra dependencies required.

    When *known_areas* is None or empty, falls back to free-text entry.
    """
    print("\n--- Alert Areas ---")

    selected: List[str] = list(existing) if existing else []

    if not known_areas:
        print("Enter Hebrew city/area names one at a time. Empty line to finish.")
        if selected:
            print(f"Current areas: {', '.join(selected)}")
        while True:
            area = input(f"  Area {len(selected) + 1}: ").strip()
            if not area:
                if selected:
                    break
                print("  At least one area is required.")
                continue
            if area not in selected:
                selected.append(area)
            print(f"  Added. ({len(selected)} selected so far)")
        print(f"\nMonitoring: {', '.join(selected)}")
        return selected

    print("Type any part of a city name to filter, then enter its number to select.")
    print("Empty line when done.\n")

    if selected:
        print(f"Current areas: {', '.join(selected)}")
        print("Add more, or press Enter to keep them:\n")

    current_matches: List[str] = []

    while True:
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not raw:
            if selected:
                break
            print("  At least one area is required.\n")
            continue

        # Number → select from the last shown matches
        if raw.isdigit():
            idx = int(raw) - 1
            if not current_matches:
                print("  No matches to select from — type a filter first.\n")
                continue
            if not (0 <= idx < len(current_matches)):
                print(f"  Enter a number between 1 and {len(current_matches)}.\n")
                continue
            area = current_matches[idx]
            if area in selected:
                print(f"  Already selected.\n")
            else:
                selected.append(area)
                print(f"  ✓ Added: {area}  ({len(selected)} area(s) total)\n")
            current_matches = []
            continue

        # Text → filter and display matches
        current_matches = [a for a in known_areas if raw.lower() in a.lower()]
        if not current_matches:
            print(f"  No cities match '{raw}' — try different text.\n")
            continue

        shown = current_matches[:_FILTER_MAX_SHOWN]
        for i, city in enumerate(shown, 1):
            print(f"  {i}. {city}")
        if len(current_matches) > _FILTER_MAX_SHOWN:
            print(f"  … {len(current_matches) - _FILTER_MAX_SHOWN} more — type more characters to narrow down")
        print()

    print(f"\nMonitoring: {', '.join(selected)}")
    return selected


async def _filter_valid_areas(areas: List[str], session) -> List[str]:
    """
    Validate *areas* against the Pikud HaOref city list.

    Returns the subset of areas that are valid (or could not be checked).
    Logs a warning for each dropped area with candidate suggestions.
    TEST_CITY_TOKEN is always kept — it is not a real city but is intentional.
    """
    known = await fetch_known_areas(session)
    if not known:
        log.debug("_filter_valid_areas: city list unavailable — keeping all areas as-is")
        return list(areas)
    bad = set(validate_configured_areas(areas, known))
    bad.discard(TEST_CITY_TOKEN)  # always keep — it's an intentional test token
    if not bad:
        log.info("Area validation OK — all configured areas matched known cities")
        return list(areas)
    for area in bad:
        suggestions = [k for k in known if any(c in k for c in area if len(c.encode()) > 1)][:5]
        hint = f"  Did you mean one of: {', '.join(suggestions)}" if suggestions else ""
        log.warning(
            "Dropping unrecognized area %r — not found in Pikud HaOref city list.%s",
            area,
            hint,
        )
    return [a for a in areas if a not in bad]


# ---------------------------------------------------------------------------
# Main service
# ---------------------------------------------------------------------------

class MamadService:
    """Async orchestrator: monitors alerts, triggers room cleaning."""

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self.rooms: List[Dict] = []
        self.is_cleaning = False
        self.cleaning_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()

        # Sub-components
        self.scheduler = RoomScheduler(
            state_file=cfg.get("state_file", "mamad_state.json"),
            exclude_rooms=cfg.get("exclude_rooms", []),
            cooldown_hours=float(cfg.get("cooldown_hours", 1.0)),
            max_cleans_per_window=int(cfg.get("max_cleans_per_room", 2)),
            clean_window_hours=float(cfg.get("clean_window_hours", 12.0)),
            selection_strategy=cfg.get("room_selection_strategy", "round_robin"),
        )
        self.vacuum_type = cfg.get("vacuum_type") or self.scheduler.get_vacuum_type()
        if self.vacuum_type == "dreame":
            self.vacuum = DreameController(min_battery_percent=int(cfg.get("min_battery_percent", 20)))
        else:
            self.vacuum = VacuumController(min_battery_percent=int(cfg.get("min_battery_percent", 20)))
        self.notifier = Notifier(cfg.get("notifications", {}))
        self.alert_monitor: Optional[AlertMonitor] = None
        configured_profile = cfg.get("cleaning_profile")
        state_profile = self.scheduler.get_cleaning_profile()
        self.cleaning_profile = _normalize_cleaning_profile(configured_profile or state_profile or "auto")

    # ------------------------------------------------------------------
    # Setup mode
    # ------------------------------------------------------------------

    async def run_setup(self) -> None:
        """Interactive first-run: auth, discover rooms, print them."""
        log.info("=== MAMAD Vacuum Setup ===")

        print("Which vacuum brand do you have?")
        print("  1. Roborock")
        print("  2. Dreame")
        choice = input("Enter choice (1 or 2): ").strip()

        if choice == "2":
            self.vacuum_type = "dreame"
            self.scheduler.set_vacuum_type("dreame")
            self.vacuum = DreameController(min_battery_percent=int(self.cfg.get("min_battery_percent", 20)))

            username = self.scheduler.get_dreame_username()
            if not username:
                username = input("Enter your Xiaomi account email or phone (for Dreame): ").strip()
                if not username:
                    sys.exit("ERROR: Username is required")

            account_type = _prompt_with_default(
                "Enter Dreame account type (mi/dreame)",
                self.scheduler.get_dreame_account_type(),
            ).lower()
            if account_type not in {"mi", "dreame"}:
                sys.exit("ERROR: Dreame account type must be 'mi' or 'dreame'")

            default_country = self.scheduler.get_dreame_country()
            if account_type == "dreame" and default_country == "cn":
                default_country = "eu"
            if account_type == "dreame":
                print("Tip: Dreamehome app routes Israel (IL) to region 'sg'.")
            country = _prompt_with_default(
                "Enter Dreame cloud country/region (e.g. de/us/cn)",
                default_country,
            ).lower()
            if not country:
                sys.exit("ERROR: Country is required")

            creds = await self.vacuum.setup(
                username=username,
                country=country,
                account_type=account_type,
                cached_credentials=self.scheduler.get_cached_credentials(),
                interactive=True,
            )
            self.scheduler.set_dreame_username(username)
            self.scheduler.set_dreame_account_type(account_type)
            self.scheduler.set_dreame_country(country)
            self.scheduler.set_cached_credentials(creds)
            self.scheduler.save()
        else:
            self.vacuum_type = "roborock"
            self.scheduler.set_vacuum_type("roborock")
            self.vacuum = VacuumController(min_battery_percent=int(self.cfg.get("min_battery_percent", 20)))

            # Use stored email if available, otherwise prompt
            email = self.scheduler.get_email()
            if not email:
                email = input("Enter your Roborock account email: ").strip()
                if not email:
                    sys.exit("ERROR: Email is required")

            creds = await self.vacuum.setup(
                email=email,
                cached_credentials=self.scheduler.get_cached_credentials(),
                interactive=True,
            )
            self.scheduler.set_email(email)
            self.scheduler.set_cached_credentials(creds)
            self.scheduler.save()

        default_profile = _normalize_cleaning_profile(self.scheduler.get_cleaning_profile() or self.cleaning_profile)
        selected_profile = _prompt_cleaning_profile(default_profile)
        self.cleaning_profile = selected_profile
        self.scheduler.set_cleaning_profile(selected_profile)
        self.scheduler.save()
        print(f"Selected cleaning behavior: {selected_profile} — {_CLEANING_PROFILES[selected_profile]}")

        # Areas setup — show known cities before prompting so the user can verify spelling
        print("\nFetching available city/area names from Pikud HaOref...")
        async with aiohttp.ClientSession() as _sess:
            known_areas = await fetch_known_areas(_sess)
        if known_areas:
            print(f"  {len(known_areas)} cities available (e.g. {', '.join(known_areas[:6])} …)")
        else:
            print("  Could not fetch city list — continuing without validation.")

        areas = _prompt_areas(
            existing=self.scheduler.get_areas() or self.cfg.get("areas", []),
            known_areas=known_areas or None,
        )

        self.scheduler.set_areas(areas)
        self.scheduler.save()

        await self.vacuum.discover_devices()
        rooms = await self.vacuum.discover_rooms()

        if not rooms:
            print("\nWARNING: No rooms discovered. The vacuum may need to complete a mapping run first.")
        else:
            self.scheduler.update_rooms(rooms)
            self.scheduler.save()
            print(f"\nDiscovered {len(rooms)} rooms:")
            for r in rooms:
                print(f"  id={r['id']:>5}  name={r['name']}")

        print("\nSetup complete. You can now start the daemon:")
        print("  python mamad_roborock.py")

    # ------------------------------------------------------------------
    # Daemon mode
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Daemon: authenticate, check state, then monitor alerts."""
        log.info("=== MAMAD Vacuum starting (daemon mode) ===")

        # Auth
        if self.vacuum_type == "dreame":
            username = self.scheduler.get_dreame_username()
            if not username:
                sys.exit(
                    "ERROR: No Xiaomi/Dreame account found in state file.\n"
                    "Run setup first:  python mamad_roborock.py --setup"
                )
            country = self.scheduler.get_dreame_country()
            account_type = self.scheduler.get_dreame_account_type()
            creds = await self.vacuum.setup(
                username=username,
                country=country,
                account_type=account_type,
                cached_credentials=self.scheduler.get_cached_credentials(),
                interactive=False,
            )
        else:
            email = self.scheduler.get_email()
            if not email:
                sys.exit(
                    "ERROR: No Roborock account found in state file.\n"
                    "Run setup first:  python mamad_roborock.py --setup"
                )
            creds = await self.vacuum.setup(
                email=email,
                cached_credentials=self.scheduler.get_cached_credentials(),
                interactive=False,
            )

        self.scheduler.set_cached_credentials(creds)
        self.scheduler.save()

        await self.vacuum.discover_devices()

        # Resolve areas: config.yaml takes priority, then state file
        areas = self.cfg.get("areas") or self.scheduler.get_areas()
        if not areas:
            sys.exit(
                "ERROR: No alert areas configured.\n"
                "Run setup first:  python mamad_roborock.py --setup"
            )

        # Test mode: also react to Pikud HaOref's own scheduled test drills.
        # The live API continuously fires alerts whose city name contains "בדיקה".
        # Most client libraries filter these out; we opt-in here to use them as
        # a free real-API end-to-end test without waiting for a real event.
        alert_types = self.cfg.get("alert_types", ["1"])
        if self.cfg.get("test_mode"):
            if TEST_CITY_TOKEN not in areas:
                areas = list(areas) + [TEST_CITY_TOKEN]
            # In test mode, accept all alert types (tests may use any category)
            alert_types = ["1", "2", "3", "4", "5", "6", "7", "8", "9"]
            log.warning(
                "TEST MODE — also watching for Pikud HaOref test drills "
                "(city contains '%s') and accepting all alert types. Disable with Ctrl-C when done testing.",
                TEST_CITY_TOKEN,
            )

        # Validate and filter areas against the Pikud HaOref city list
        async with aiohttp.ClientSession() as _session:
            areas = await _filter_valid_areas(areas, _session)

        if not areas:
            sys.exit(
                "ERROR: No valid alert areas remain after validation.\n"
                "Run setup again:  python mamad_roborock.py --setup"
            )

        self.alert_monitor = AlertMonitor(
            areas=areas,
            poll_seconds=int(self.cfg.get("poll_seconds", 5)),
            alert_types=alert_types,
        )

        # Refresh rooms if cache is stale
        await self._refresh_rooms_if_stale()

        if not self.rooms:
            log.error("No rooms available — check setup or re-run with --setup")
            return

        # Check if vacuum is already cleaning on startup
        await self._handle_startup_state()

        # Register graceful shutdown handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._request_shutdown)

        log.info("Monitoring alerts for areas: %s", self.alert_monitor.areas)
        log.info("Rooms in rotation: %s", [(r["id"], r["name"]) for r in self.rooms])

        # Start alert polling (runs until self._shutdown_event is set)
        monitor_task = asyncio.create_task(
            self.alert_monitor.start(callback=self.on_alert)
        )
        await self._shutdown_event.wait()

        # Graceful shutdown
        log.info("Shutdown requested — stopping...")
        self.alert_monitor.stop()
        monitor_task.cancel()

        if self.cleaning_task and not self.cleaning_task.done():
            log.info("Cancelling active cleaning task...")
            self.cleaning_task.cancel()
            try:
                await self.cleaning_task
            except asyncio.CancelledError:
                pass
            # Best-effort dock
            try:
                await self.vacuum.stop_and_dock()
            except Exception as exc:
                log.error("stop_and_dock on shutdown failed: %s", exc)

        self.scheduler.save()
        await self.vacuum.close()
        log.info("MAMAD Roborock stopped cleanly")

    def _request_shutdown(self) -> None:
        log.info("Signal received — requesting shutdown")
        self._shutdown_event.set()

    # ------------------------------------------------------------------
    # Alert callback
    # ------------------------------------------------------------------

    async def on_alert(self, alert: Dict) -> None:
        """Called by AlertMonitor when a new matching alert fires."""
        alert_id = str(alert.get("id", "?"))
        cities = alert.get("data", [])
        log.info("ALERT id=%s cities=%s", alert_id, cities)

        # Query actual vacuum status — more reliable than checking self.is_cleaning flag
        try:
            status = await self.vacuum.get_status()
            result = status["result"]
        except Exception as exc:
            log.warning("Could not get vacuum status for alert id=%s: %s", alert_id, exc)
            await self.notifier.send(f"Alert received but couldn't check vacuum status: {exc}")
            return

        if result == STATUS_ALREADY_CLEANING:
            log.info("Alert ignored — vacuum is actively cleaning (alert id=%s, state=%s)",
                     alert_id, status["state"])
            await self.notifier.send(f"Alert received but vacuum is already cleaning (id={alert_id})")
            return

        if result != STATUS_OK:
            log.warning("Skipping clean for alert id=%s — vacuum status: %s (state=%s, battery=%d%%)",
                        alert_id, result, status["state"], status["battery"])
            await self.notifier.send(
                f"Alert received but vacuum unavailable: {result} "
                f"(state={status['state']}, battery={status['battery']}%)"
            )
            return

        room = self.scheduler.get_next_room(self.rooms)
        if room is None:
            log.warning("No eligible rooms available for alert id=%s", alert_id)
            await self.notifier.send("Alert received but no eligible rooms available for cleaning")
            return

        self.is_cleaning = True
        self.cleaning_task = asyncio.create_task(self._run_clean(room, alert_id))

    # ------------------------------------------------------------------
    # Cleaning session
    # ------------------------------------------------------------------

    async def _run_clean(self, room: Dict, alert_id: str) -> None:
        """Run a timed cleaning session for *room*."""
        duration_seconds = float(self.cfg.get("clean_duration_minutes", 10)) * 60
        fan_speed = self.cfg.get("fan_speed", "balanced")
        room_name = room.get("name", f"Room {room['id']}")

        log.info("Starting cleaning session: room=%s id=%s duration=%.0fs",
                 room_name, room["id"], duration_seconds)
        await self.notifier.send(
            f"🧹 Cleaning *{room_name}* for {self.cfg.get('clean_duration_minutes', 10)} minutes "
            f"(alert id={alert_id})"
        )

        try:
            if self.cleaning_profile == "auto":
                await self.vacuum.start_segment_clean(room["id"], fan_speed=fan_speed)
            else:
                await self.vacuum.start_segment_clean(
                    room["id"], fan_speed=fan_speed, cleaning_profile=self.cleaning_profile
                )
            log.info("Cleaning started — waiting %.0f seconds", duration_seconds)
            await asyncio.sleep(duration_seconds)
            log.info("Clean duration elapsed — stopping")
            await self.vacuum.stop_and_dock()

            self.scheduler.mark_cleaned(room["id"])
            self.scheduler.save()

            log.info("Cleaning session complete: room=%s", room_name)
            await self.notifier.send(f"✅ Done cleaning *{room_name}* — returned to dock")

        except asyncio.CancelledError:
            log.info("Cleaning task cancelled for room=%s", room_name)
            raise
        except Exception as exc:
            log.error("Cleaning session failed for room=%s: %s", room_name, exc)
            await self.notifier.send(f"❌ Cleaning *{room_name}* failed: {exc}")
        finally:
            self.is_cleaning = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _refresh_rooms_if_stale(self) -> None:
        """Discover rooms from API or load from cache."""
        if self.scheduler.is_room_cache_stale():
            log.info("Room cache is stale — discovering rooms from API")
            try:
                rooms = await self.vacuum.discover_rooms()
                if rooms:
                    self.scheduler.update_rooms(rooms)
                    self.scheduler.save()
            except Exception as exc:
                log.warning("Room discovery failed: %s — using cached data", exc)

        self.rooms = self.scheduler.get_cached_rooms()
        if not self.rooms:
            log.warning("No rooms in cache and discovery failed — run --setup first")

    async def _handle_startup_state(self) -> None:
        """If vacuum is already cleaning at startup, wait for it to finish."""
        try:
            status = await self.vacuum.get_status()
        except Exception as exc:
            log.warning("Could not get vacuum status at startup: %s", exc)
            return

        if status["result"] == STATUS_ALREADY_CLEANING:
            log.info(
                "Vacuum is already cleaning at startup (state=%s) — "
                "ignoring alerts until it finishes",
                status["state"],
            )
            self.is_cleaning = True
            self.cleaning_task = asyncio.create_task(self._wait_for_idle())

    async def _wait_for_idle(self) -> None:
        """Poll until the vacuum is no longer in a cleaning state."""
        poll_interval = 30
        log.info("Waiting for vacuum to finish current session (polling every %ds)", poll_interval)
        while True:
            await asyncio.sleep(poll_interval)
            try:
                status = await self.vacuum.get_status()
                if status["result"] != STATUS_ALREADY_CLEANING:
                    log.info(
                        "Vacuum idle again (state=%s) — resuming alert monitoring",
                        status["state"],
                    )
                    break
            except Exception as exc:
                log.warning("Status poll error while waiting for idle: %s", exc)
        self.is_cleaning = False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _test_alert(cfg: Dict[str, Any], scheduler: "RoomScheduler") -> None:
    """Poll the alert API once and show exactly what was returned."""
    import json
    import aiohttp
    from alert_monitor import ALERTS_URL, HEADERS, _cache_bust_url, _decode_response

    areas = cfg.get("areas") or scheduler.get_areas()
    if not areas:
        sys.exit("ERROR: No areas configured. Run --setup first.")
    alert_types = cfg.get("alert_types", ["1"])

    print(f"Polling {ALERTS_URL}")
    print(f"Watching areas: {areas}\n")

    async with aiohttp.ClientSession() as session:
        async with session.get(_cache_bust_url(ALERTS_URL), headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            raw = await resp.read()
            text = _decode_response(raw).strip()

    if not text:
        print("No active alert (empty response).")
        return

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        print(f"Non-JSON response: {text!r}")
        return

    print("Raw alert data:")
    print(json.dumps(data, ensure_ascii=False, indent=2))

    cat = str(data.get("cat", ""))
    cities = data.get("data", [])
    cat_match = cat in alert_types
    area_match = any(
        area.lower() in city.lower()
        for area in areas
        for city in cities
    )

    print(f"\nCategory '{cat}' in configured types {alert_types}: {cat_match}")
    print(f"Area match for {areas}: {area_match}")
    if cat_match and area_match:
        print("\n✓ This alert WOULD trigger cleaning.")
    else:
        print("\n✗ This alert would NOT trigger cleaning.")


async def _list_areas(filter_str: str = None) -> None:
    """Fetch and print all Pikud HaOref city/area names, with optional substring filter."""
    async with aiohttp.ClientSession() as session:
        areas = await fetch_known_areas(session)

    if not areas:
        print("ERROR: Could not fetch area list from Pikud HaOref.")
        return

    if filter_str:
        areas = [a for a in areas if filter_str.lower() in a.lower()]
        print(f"Areas matching {filter_str!r} ({len(areas)} found):")
    else:
        print(f"All Pikud HaOref alert areas ({len(areas)} total):")

    for area in sorted(areas):
        print(f"  {area}")


async def _async_main(args: argparse.Namespace) -> None:
    cfg = _load_config(args.config)
    _setup_logging(cfg)

    service = MamadService(cfg)

    if args.test_mode:
        cfg["test_mode"] = True

    if args.setup:
        await service.run_setup()
    elif args.list_areas:
        await _list_areas(args.filter)
    elif args.test_clean is not None:
        await service.run_test_clean(args.test_clean)
    elif args.inject_alert:
        await service.run_inject_alert(args.inject_alert)
    elif args.test_alert:
        await _test_alert(cfg, service.scheduler)
    else:
        await service.run()


async def _run_test_clean(self, room_id: int) -> None:
    """Connect, clean one room for 30 s, then dock. Used by --test-clean."""
    print(f"\nTest clean: room id={room_id} for 30 seconds\n")

    if self.vacuum_type == "dreame":
        username = self.scheduler.get_dreame_username()
        if not username:
            sys.exit("ERROR: Run --setup first")
        await self.vacuum.setup(
            username=username,
            country=self.scheduler.get_dreame_country(),
            account_type=self.scheduler.get_dreame_account_type(),
            cached_credentials=self.scheduler.get_cached_credentials(),
            interactive=False,
        )
    else:
        email = self.scheduler.get_email()
        if not email:
            sys.exit("ERROR: Run --setup first")

        await self.vacuum.setup(
            email=email,
            cached_credentials=self.scheduler.get_cached_credentials(),
            interactive=False,
        )

    await self.vacuum.discover_devices()

    status = await self.vacuum.get_status()
    print(f"Vacuum status: state={status['state']} battery={status['battery']}%")
    if status["result"] != STATUS_OK:
        sys.exit(f"ERROR: Vacuum not ready — {status['result']}")

    rooms = self.scheduler.get_cached_rooms()
    room = next((r for r in rooms if r["id"] == room_id), None)
    if room is None:
        available = ", ".join(f"{r['id']}={r['name']}" for r in rooms)
        sys.exit(f"ERROR: Room id={room_id} not found. Available: {available}")

    print(f"Starting segment clean: {room['name']} (id={room_id})")
    if self.cleaning_profile == "auto":
        await self.vacuum.start_segment_clean(room_id, fan_speed=self.cfg.get("fan_speed", "balanced"))
    else:
        await self.vacuum.start_segment_clean(
            room_id,
            fan_speed=self.cfg.get("fan_speed", "balanced"),
            cleaning_profile=self.cleaning_profile,
        )

    print("Cleaning for 30 seconds...")
    await asyncio.sleep(30)

    print("Stopping and returning to dock...")
    await self.vacuum.stop_and_dock()
    await self.vacuum.close()
    print("Done.")


# Attach as method
MamadService.run_test_clean = _run_test_clean


async def _run_inject_alert(self, city: str) -> None:
    """
    Inject a synthetic alert for *city* and run the full on_alert() pipeline.

    Unlike --test-clean (which skips the alert logic), this exercises the
    complete chain: status check → room scheduler → start_segment_clean →
    clean duration → stop_and_dock → mark_cleaned.

    Tip: set clean_duration_minutes to 0.5 in config.yaml for a quick test.
    """
    print(f"\nInjecting synthetic alert for city: '{city}'")
    print("This runs the full alert → vacuum pipeline.\n")

    if self.vacuum_type == "dreame":
        username = self.scheduler.get_dreame_username()
        if not username:
            sys.exit("ERROR: Run --setup first")
        creds = await self.vacuum.setup(
            username=username,
            country=self.scheduler.get_dreame_country(),
            account_type=self.scheduler.get_dreame_account_type(),
            cached_credentials=self.scheduler.get_cached_credentials(),
            interactive=False,
        )
    else:
        email = self.scheduler.get_email()
        if not email:
            sys.exit("ERROR: Run --setup first")

        creds = await self.vacuum.setup(
            email=email,
            cached_credentials=self.scheduler.get_cached_credentials(),
            interactive=False,
        )

    self.scheduler.set_cached_credentials(creds)
    self.scheduler.save()

    await self.vacuum.discover_devices()
    await self._refresh_rooms_if_stale()

    if not self.rooms:
        sys.exit("ERROR: No rooms in cache — run --setup first")

    print(f"Rooms in rotation: {[(r['id'], r['name']) for r in self.rooms]}")
    print(f"Clean duration: {self.cfg.get('clean_duration_minutes', 10)} min "
          f"(set clean_duration_minutes in config.yaml to shorten for testing)\n")

    alert = {
        "id": "SYNTHETIC-TEST-001",
        "cat": "1",
        "title": "ירי רקטות וטילים [TEST]",
        "data": [city],
        "desc": "Injected by --inject-alert for local testing",
    }
    log.info("Injecting synthetic alert: %s", json.dumps(alert, ensure_ascii=False))

    await self.on_alert(alert)

    if not self.cleaning_task:
        print("\nAlert was received but no cleaning started — check the log for the reason.")
        print("Common causes: vacuum not ready, no eligible rooms, city didn't match configured areas.")
    else:
        print("Cleaning task started — waiting for it to finish...")
        await self.cleaning_task
        print("\nDone.")

    await self.vacuum.close()


MamadService.run_inject_alert = _run_inject_alert


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MAMAD Roborock — Missile Alert Auto-Cleaner"
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        metavar="PATH",
        help="Path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Run interactive setup (auth, areas, room discovery). Re-run anytime to change settings.",
    )
    parser.add_argument(
        "--test-clean",
        type=int,
        metavar="ROOM_ID",
        help="Clean a room for 30 s then dock (e.g. --test-clean 16 for Kitchen)",
    )
    parser.add_argument(
        "--test-mode",
        action="store_true",
        help=(
            "Run the daemon in test mode: also react to Pikud HaOref's own scheduled "
            "test drills (city name contains 'בדיקה'). These fire continuously on the "
            "live API. Use this to verify the full alert → vacuum pipeline with real "
            "API traffic. Watch the log with: tail -f mamad.log"
        ),
    )
    parser.add_argument(
        "--inject-alert",
        metavar="CITY",
        default=None,
        help=(
            "Inject a synthetic alert for CITY and run the full alert → vacuum pipeline. "
            "Use a city name that matches your configured areas, e.g. --inject-alert קדימה-צורן. "
            "Tip: set clean_duration_minutes: 0.5 in config.yaml for a 30-second test clean."
        ),
    )
    parser.add_argument(
        "--test-alert",
        action="store_true",
        help="Poll the alert API once and show whether it matches your configured areas",
    )
    parser.add_argument(
        "--list-areas",
        action="store_true",
        help="List all city/area names known to Pikud HaOref, then exit",
    )
    parser.add_argument(
        "--filter",
        metavar="TEXT",
        default=None,
        help="Substring filter for --list-areas (e.g. --filter תל)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
