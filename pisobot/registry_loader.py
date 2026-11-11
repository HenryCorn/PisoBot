"""Where optional features register their loaders and plain startup templates.

Imported during foundation initialization in the parent and child. Do not import feature modules here: their loaders run on workers.
"""
import importlib

from . import optional

TIMING_TEMPLATES = (
    {"field": "first_listed_at", "kind": "age", "text": "🕒 Listed {} ago", "priority": 5, "max_seconds": 3 * 3600},
)

optional.register("ALERT_TIMING", lambda: importlib.import_module("pisobot.timing").FEATURE,
                  templates=TIMING_TEMPLATES, empty_cycle=True)
