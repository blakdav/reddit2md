"""User-editable settings, stored in /data/settings.json. Environment variables only supply first-run defaults."""

import json
import os
import threading
from zoneinfo import ZoneInfo, available_timezones

SETTINGS_FILE = os.environ.get("SETTINGS_FILE", "/data/settings.json")
_lock = threading.Lock()


def _default_tz():
    tz = os.environ.get("TZ", "").strip()
    try:
        ZoneInfo(tz)
        return tz
    except Exception:
        return "UTC"


DEFAULTS = {
    "retention_days": int(float(os.environ.get("RETENTION_DAYS", "30"))),
    "timezone": _default_tz(),
}


def timezones():
    return sorted(z for z in available_timezones() if "/" in z or z == "UTC")


def load():
    data = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except (FileNotFoundError, ValueError):
        pass
    return data


def save(updates):
    data = load()
    if "retention_days" in updates:
        try:
            days = int(updates["retention_days"])
        except (TypeError, ValueError):
            raise ValueError("Auto-delete days must be a whole number")
        if not 0 <= days <= 3650:
            raise ValueError("Auto-delete days must be between 0 and 3650")
        data["retention_days"] = days
    if "timezone" in updates:
        tz = str(updates["timezone"])
        if tz not in available_timezones():
            raise ValueError(f"Unknown time zone: {tz}")
        data["timezone"] = tz
    with _lock:
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    return data
