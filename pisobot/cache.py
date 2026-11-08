"""Atomic JSON cache; PISOBOT_STATE_DIR overrides the historical repo location."""
import json
import os
from pathlib import Path
import uuid

from . import localdb

PATH = (os.path.join(os.environ["PISOBOT_STATE_DIR"], "cache.json")
        if os.environ.get("PISOBOT_STATE_DIR")
        else os.path.join(os.path.dirname(__file__), "..", "state", "cache.json"))
_data = None


def _load():
    global _data
    if _data is None:
        local = localdb.storage() == "local"
        try:
            with open(PATH, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            if local:
                raise RuntimeError(f"Invalid local cache at {PATH}: {error}") from error
            if isinstance(error, UnicodeDecodeError):
                raise
            data = {}
        if local and (not isinstance(data, dict)
                      or any(not isinstance(value, dict) for value in data.values())):
            raise RuntimeError(f"Invalid local cache at {PATH}: expected namespace objects")
        _data = data
    return _data


def get(ns, key):
    return _load().get(ns, {}).get(key)


def put(ns, key, value):
    _load().setdefault(ns, {})[key] = value


def items(ns):
    return _load().get(ns, {}).items()


def save():
    if _data is None:
        return
    path = Path(PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    try:
        with pending.open("x", encoding="utf-8") as f:
            json.dump(_data, f, indent=0, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)
