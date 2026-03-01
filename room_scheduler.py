"""
room_scheduler.py — Round-robin room selector with persistent state.

State is stored in mamad_state.json (path configurable).
The file is chmod-600 on creation to protect cached Roborock credentials
that are stored alongside the scheduling state.
"""

import json
import logging
import os
import stat
import unicodedata
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

log = logging.getLogger(__name__)

_ISO_FMT = "%Y-%m-%dT%H:%M:%S.%f+00:00"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


class RoomScheduler:
    """
    Selects the next room to clean in round-robin order.

    State file schema (mamad_state.json):
    {
        "round_robin_index": 0,
        "discovered_rooms": [{"id": 16, "name": "Living Room"}, ...],
        "last_room_discovery": "<ISO timestamp>",
        "last_cleaned": {"16": "<ISO timestamp>", ...},
        "total_alert_cleans": 0,
        "last_alert_id": null,
        "last_alert_time": null,
        "roborock_cached_credentials": {}
    }
    """

    def __init__(
        self,
        state_file: str = "mamad_state.json",
        exclude_rooms: List[str] = None,
        cooldown_hours: float = 1.0,
        room_cache_hours: float = 24.0,
        max_cleans_per_window: int = 2,
        clean_window_hours: float = 12.0,
        selection_strategy: str = "round_robin",
    ) -> None:
        self.state_file = state_file
        self.exclude_rooms = [r.lower() for r in (exclude_rooms or [])]
        self.cooldown_hours = cooldown_hours
        self.room_cache_hours = room_cache_hours
        self.max_cleans_per_window = max_cleans_per_window
        self.clean_window_hours = clean_window_hours
        self.selection_strategy = (selection_strategy or "round_robin").strip().lower()
        if self.selection_strategy not in {"round_robin", "oldest_cleaned"}:
            log.warning("Unknown selection_strategy=%r, falling back to round_robin", self.selection_strategy)
            self.selection_strategy = "round_robin"
        self._state: Dict[str, Any] = {}
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_next_room(self, rooms: List[Dict]) -> Optional[Dict]:
        """
        Return the next eligible room from *rooms* (round-robin, with cooldown).

        *rooms* is a list of ``{"id": int, "name": str}`` dicts.
        Returns None if no eligible room is found.
        Advances the round-robin index.
        """
        if not rooms:
            return None

        eligible = self._eligible_rooms(rooms)
        if not eligible:
            log.warning("All rooms are on cooldown, rate-limited, or excluded — skipping")
            return None

        if self.selection_strategy == "oldest_cleaned":
            room = min(
                eligible,
                key=lambda r: (
                    self._last_cleaned_dt(r["id"]) or datetime.min.replace(tzinfo=timezone.utc),
                    int(r["id"]),
                ),
            )
            log.info(
                "Selected room id=%s name=%s (strategy=oldest_cleaned)",
                room["id"],
                room["name"],
            )
            return room

        idx = self._state.get("round_robin_index", 0) % len(eligible)
        room = eligible[idx]
        # Advance index for next call
        self._state["round_robin_index"] = (idx + 1) % len(eligible)
        log.info("Selected room id=%s name=%s (index %d/%d)", room["id"], room["name"], idx, len(eligible))
        return room

    def mark_cleaned(self, room_id: int) -> None:
        """Record that *room_id* was just cleaned."""
        now = _now_iso()
        last_cleaned = self._state.setdefault("last_cleaned", {})
        last_cleaned[str(room_id)] = now
        # Append to rate-limit history
        self._state.setdefault("clean_history", {}).setdefault(str(room_id), []).append(now)
        self._state["total_alert_cleans"] = self._state.get("total_alert_cleans", 0) + 1
        log.info("Marked room %s as cleaned (total cleans: %d)", room_id, self._state["total_alert_cleans"])

    def update_rooms(self, rooms: List[Dict]) -> None:
        """Cache the discovered rooms and update the discovery timestamp."""
        self._state["discovered_rooms"] = rooms
        self._state["last_room_discovery"] = _now_iso()
        log.info("Updated room cache with %d rooms", len(rooms))

    def is_room_cache_stale(self) -> bool:
        """Return True if rooms were never discovered or cache is older than room_cache_hours."""
        ts_str = self._state.get("last_room_discovery")
        if not ts_str:
            return True
        ts = _parse_iso(ts_str)
        if ts is None:
            return True
        age = datetime.now(timezone.utc) - ts
        return age > timedelta(hours=self.room_cache_hours)

    def get_cached_rooms(self) -> List[Dict]:
        return self._state.get("discovered_rooms", [])

    def get_email(self) -> Optional[str]:
        return self._state.get("roborock_email")

    def set_email(self, email: str) -> None:
        self._state["roborock_email"] = email

    def get_dreame_username(self) -> Optional[str]:
        return self._state.get("dreame_username")

    def set_dreame_username(self, username: str) -> None:
        self._state["dreame_username"] = username

    def get_dreame_country(self) -> str:
        return self._state.get("dreame_country", "cn")

    def set_dreame_country(self, country: str) -> None:
        self._state["dreame_country"] = (country or "cn").strip().lower()

    def get_dreame_account_type(self) -> str:
        return self._state.get("dreame_account_type", "mi")

    def set_dreame_account_type(self, account_type: str) -> None:
        self._state["dreame_account_type"] = (account_type or "mi").strip().lower()

    def get_vacuum_type(self) -> Optional[str]:
        return self._state.get("vacuum_type", "roborock")

    def set_vacuum_type(self, vtype: str) -> None:
        self._state["vacuum_type"] = vtype

    def get_cached_credentials(self) -> Dict:
        return self._state.get("roborock_cached_credentials", {})

    def set_cached_credentials(self, creds: Dict) -> None:
        self._state["roborock_cached_credentials"] = creds

    def get_areas(self) -> List[str]:
        return self._state.get("areas", [])

    def set_areas(self, areas: List[str]) -> None:
        self._state["areas"] = areas

    def get_cleaning_profile(self) -> str:
        return self._state.get("cleaning_profile", "auto")

    def set_cleaning_profile(self, profile: str) -> None:
        self._state["cleaning_profile"] = (profile or "auto").strip().lower()

    def get_last_alert_id(self) -> Optional[str]:
        return self._state.get("last_alert_id")

    def set_last_alert_id(self, alert_id: str) -> None:
        self._state["last_alert_id"] = alert_id
        self._state["last_alert_time"] = _now_iso()

    def save(self) -> None:
        """Persist state to disk (chmod 600)."""
        tmp = self.state_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp, self.state_file)
            # Restrict permissions: owner read/write only
            os.chmod(self.state_file, stat.S_IRUSR | stat.S_IWUSR)
            log.debug("State saved to %s", self.state_file)
        except OSError as exc:
            log.error("Failed to save state: %s", exc)
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not os.path.exists(self.state_file):
            log.info("No state file found at %s — starting fresh", self.state_file)
            self._state = self._empty_state()
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("State file is not a JSON object")
            self._state = {**self._empty_state(), **data}
            log.info("Loaded state from %s (index=%d, total_cleans=%d)",
                     self.state_file,
                     self._state.get("round_robin_index", 0),
                     self._state.get("total_alert_cleans", 0))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            log.warning("State file corrupt/unreadable (%s) — resetting", exc)
            self._state = self._empty_state()

    @staticmethod
    def _empty_state() -> Dict[str, Any]:
        return {
            "round_robin_index": 0,
            "discovered_rooms": [],
            "last_room_discovery": None,
            "last_cleaned": {},
            "clean_history": {},
            "total_alert_cleans": 0,
            "last_alert_id": None,
            "last_alert_time": None,
            "roborock_email": None,
            "dreame_username": None,
            "dreame_country": "cn",
            "dreame_account_type": "mi",
            "vacuum_type": "roborock",
            "cleaning_profile": "auto",
            "roborock_cached_credentials": {},
            "areas": [],
        }

    # Exact names that are always excluded — the safe room should never be
    # cleaned during an alert.
    _MAMAD_NAMES = {"mamad", "ממד", 'ממ"ד', "ממ״ד"}
    _MAMAD_NORMALIZED = None

    @staticmethod
    def _normalize_room_name(name: str) -> str:
        """
        Normalize room names for robust exact matching across quote/Unicode variants.
        """
        if not isinstance(name, str):
            return ""

        normalized = unicodedata.normalize("NFKC", name).strip().lower()
        # Remove Hebrew/ASCII quote variants and whitespace for stable comparison.
        for ch in ['"', "'", "״", "׳", "`", "’", "“", "”", " "]:
            normalized = normalized.replace(ch, "")
        return normalized

    @classmethod
    def _is_mamad_room(cls, name: str) -> bool:
        if cls._MAMAD_NORMALIZED is None:
            cls._MAMAD_NORMALIZED = {cls._normalize_room_name(n) for n in cls._MAMAD_NAMES}
        return cls._normalize_room_name(name) in cls._MAMAD_NORMALIZED

    def _clean_count_in_window(self, room_id: int) -> int:
        """Return the number of times *room_id* was cleaned within clean_window_hours."""
        history = self._state.get("clean_history", {}).get(str(room_id), [])
        if not history:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.clean_window_hours)
        recent = [ts for ts in history if (parsed := _parse_iso(ts)) is not None and parsed >= cutoff]
        # Prune stale entries to keep the state file compact
        if len(recent) != len(history):
            self._state.setdefault("clean_history", {})[str(room_id)] = recent
        return len(recent)

    def _last_cleaned_dt(self, room_id: int) -> Optional[datetime]:
        ts = self._state.get("last_cleaned", {}).get(str(room_id))
        return _parse_iso(ts)

    def _eligible_rooms(self, rooms: List[Dict]) -> List[Dict]:
        """Filter rooms: remove excluded names, rooms on cooldown, and rate-limited rooms."""
        now = datetime.now(timezone.utc)
        cooldown_delta = timedelta(hours=self.cooldown_hours)
        eligible = []
        for room in rooms:
            name = room.get("name", "")
            # Always exclude the mamad (safe room)
            if self._is_mamad_room(name):
                log.debug("Room '%s' excluded (mamad/safe room)", name)
                continue
            # Check user exclusion list
            if any(excl in name.lower() for excl in self.exclude_rooms):
                log.debug("Room %s excluded by name filter", name)
                continue
            # Check cooldown
            last_ts_str = self._state.get("last_cleaned", {}).get(str(room["id"]))
            if last_ts_str:
                last_ts = _parse_iso(last_ts_str)
                if last_ts and (now - last_ts) < cooldown_delta:
                    remaining = cooldown_delta - (now - last_ts)
                    log.debug("Room %s on cooldown for %.0f more minutes", name, remaining.total_seconds() / 60)
                    continue
            # Check rate limit (max cleans per rolling window)
            if self.max_cleans_per_window > 0:
                count = self._clean_count_in_window(room["id"])
                if count >= self.max_cleans_per_window:
                    log.debug(
                        "Room %s rate-limited (%d/%d cleans in last %.0fh)",
                        name, count, self.max_cleans_per_window, self.clean_window_hours,
                    )
                    continue
            eligible.append(room)
        return eligible
