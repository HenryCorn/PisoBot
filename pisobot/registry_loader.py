"""Register shipped features without importing feature code until their enabled worker loads it."""
import os

from . import optional


def register():
    if os.environ.get("PLACE_FACTS") == "1":
        optional.register("PLACE_FACTS", _load_place_facts)


def _load_place_facts():
    from .place_feature import PlaceFeature
    return PlaceFeature()


register()
