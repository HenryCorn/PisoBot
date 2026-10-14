"""Tiny JSON cache persisted in the repo between GitHub Actions runs."""
import json
import os

PATH = os.path.join(os.path.dirname(__file__), "..", "state", "cache.json")
_data = None


def _load():
    global _data
    if _data is None:
        try:
            with open(PATH, encoding="utf-8") as f:
                _data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _data = {}
    return _data


def get(ns, key):
    return _load().get(ns, {}).get(key)


def put(ns, key, value):
    _load().setdefault(ns, {})[key] = value


def save():
    if _data is None:
        return
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(_data, f, indent=0, sort_keys=True)
