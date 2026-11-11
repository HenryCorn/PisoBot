"""Where the shipped optional features register themselves. Registration only records (flag, loader); the feature's module is
imported when its flag is "1" and not before."""
import importlib

from . import optional

optional.register("ALERT_TIMING", lambda: importlib.import_module("pisobot.timing").FEATURE)
