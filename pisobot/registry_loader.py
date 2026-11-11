"""Where optional features register their loaders and plain startup templates.

Imported during foundation initialization in the parent and child. Do not import feature modules here: their loaders run on workers.
"""
import importlib

from . import optional

optional.register("ALERT_TIMING", lambda: importlib.import_module("pisobot.timing").FEATURE)
