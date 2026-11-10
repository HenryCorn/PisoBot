"""The market archive: adverts, observations, verdicts, sweeps and gone detection (no network)."""
import datetime as dt
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pisobot import archive, cache, config, localdb, net
from pisobot.models import Listing
from pisobot.sources import PRIVATE_RENT, private_rent_costs

SCOPE = {"miles": 10.0, "min_rent": 1000, "max_rent": 1760, "min_beds": 1, "centre": [51.5152, -0.1419]}
T0 = "2026-11-10T09:00:00+00:00"


def advert(i=1, rent=1400, source="rm", **kw):
    values = dict(source=source, id=str(i), url=f"https://example.com/{i}", address=f"Home {i}, London E5",
                  bedrooms=1, scheme=PRIVATE_RENT, tenure="rent", rent=rent, lat=51.52, lng=-0.14)
    values.update(kw)
    return private_rent_costs(Listing(**values))


def at(hours):
    return (dt.datetime.fromisoformat(T0) + dt.timedelta(hours=hours)).isoformat(timespec="seconds")


class ArchiveBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target in (
            patch.dict(os.environ, {"STORAGE": "local", "PISOBOT_STATE_DIR": self.tmp.name,
                                    "TELEGRAM_CHAT_ID": "100", "TELEGRAM_INVITE_USERNAMES": "", "MAX_USERS": "2"}),
            patch.object(cache, "PATH", str(Path(self.tmp.name) / "cache.json")),
            patch.object(cache, "_data", {}),
            # Nothing in these tests may reach a portal, TfL or Telegram
            patch.object(net, "page", side_effect=AssertionError("live portal request")),
            patch.object(net, "get", side_effect=AssertionError("live request")),
            patch.object(net.session, "get", side_effect=AssertionError("live request")),
            patch.object(net.session, "post", side_effect=AssertionError("live request")),
        ):
            target.start()
            self.addCleanup(target.stop)


class AdvertTests(ArchiveBase):
    def test_upsert_stores_facts_and_observes_rent_changes_only(self):
        archive.upsert([advert(1, 1400, first_listed="2026-11-08")], "head", at=at(0))
        row = archive.advert("rm:1")
        self.assertEqual((row["status"], row["rent"], row["first_seen"], row["portal_first_listed"], row["head_seen"]),
                         ("active", 1400, at(0), "2026-11-08", 1))
        archive.upsert([advert(1, 1400)], "head", at=at(1))                 # nothing changed
        self.assertEqual(len(archive.history("rm:1")), 1)
        archive.upsert([advert(1, 1350)], "head", at=at(2))                 # price cut
        archive.upsert([advert(1, 1350)], "head", at=at(3))
        self.assertEqual([(h["at"], h["rent"], h["status"]) for h in archive.history("rm:1")],
                         [(at(0), 1400, "active"), (at(2), 1350, "active")])
        row = archive.advert("rm:1")
        self.assertEqual((row["first_seen"], row["last_seen"], row["portal_first_listed"]), (at(0), at(3), "2026-11-08"))

    def test_the_listing_can_be_restored_from_the_archive(self):
        archive.upsert([advert(1, 1400, agent="Foxtons", bathrooms=2, council_tax_band="C")], "head", at=at(0))
        [again] = archive.pending("100", ["rm"], "s", 10)
        self.assertEqual((again.key, again.rent, again.agent, again.bathrooms, again.council_tax_band),
                         ("rm:1", 1400, "Foxtons", 2, "C"))

    def test_sweep_counts_each_advert_once_and_flags_the_ones_the_head_poll_never_showed(self):
        archive.upsert([advert(1), advert(2)], "head", at=at(0))
        sweep = archive.start_sweep("rm", SCOPE, {}, at(1))
        first = archive.upsert([advert(1), advert(2), advert(3)], "sweep", sweep, at(1))
        again = archive.upsert([advert(3), advert(4)], "sweep", sweep, at(1))     # 3 repeats across pages
        self.assertEqual((first["seen"], first["unseen_by_head"], again["seen"], again["unseen_by_head"]),
                         (3, 1, 1, 1))
        row = archive.running_sweep("rm")
        self.assertEqual((row["adverts_seen"], row["unseen_by_head"]), (4, 2))
        self.assertEqual(archive.recall(row), 0.5)
        archive.upsert([advert(3)], "head", at=at(2))      # the head poll sees it later: the sweep's figure stays
        self.assertEqual(archive.running_sweep("rm")["unseen_by_head"], 2)

    def test_adverts_without_a_rent_are_not_archived(self):
        archive.upsert([advert(1, rent=None)], "head")
        self.assertIsNone(archive.advert("rm:1"))

    def test_archive_lives_beside_the_schema_without_touching_it(self):
        archive.upsert([advert(1)], "head")
        self.assertNotIn("adverts", localdb.SCHEMA.read_text())
        self.assertEqual(archive.counts(), {"active": 1})


class VerdictTests(ArchiveBase):
    def test_latest_outcome_wins_and_old_ones_age_out_of_the_window(self):
        archive.record_verdicts("100", [("rm:1", "rent_cap", 1650, None, None, None)], "s1", at(0))
        archive.record_verdicts("100", [("rm:1", "matched", 1650, 1933, 31, 40), ("rm:2", "travel", 1500, None, 47, None)],
                                "s1", at(24))
        archive.record_verdicts("200", [("rm:1", "crime", 1500, None, 20, 300)], "s2", at(24))
        rows = {r["key"]: r for r in archive.verdicts_since("100", 7, at(48))}
        self.assertEqual({k: r["outcome"] for k, r in rows.items()}, {"rm:1": "matched", "rm:2": "travel"})
        self.assertEqual((rows["rm:1"]["all_in"], rows["rm:1"]["travel_minutes"], rows["rm:1"]["crime_score"]),
                         (1933, 31, 40))
        self.assertEqual(len(archive.verdicts_since("100", 7, at(24 * 9))), 0)          # 7 days gone by
        self.assertEqual([r["outcome"] for r in archive.verdicts_since("200", 7, at(48))], ["crime"])

    def test_pending_skips_judged_adverts_but_not_other_settings_or_retries(self):
        archive.upsert([advert(1), advert(2), advert(3), advert(4)], "head", at=at(0))
        archive.record_verdicts("100", [("rm:1", "matched", 1400, None, None, None),
                                        ("rm:2", "enrichment", 1400, None, None, None),
                                        ("rm:3", "unsent", 1400, None, None, None)], "s1")
        keys = lambda settings, **kw: sorted(l.key for l in archive.pending("100", ["rm"], settings, 10, **kw))
        self.assertEqual(keys("s1"), ["rm:2", "rm:3", "rm:4"])
        self.assertEqual(keys("s2"), ["rm:1", "rm:2", "rm:3", "rm:4"])         # settings changed: judge again
        self.assertEqual(keys("s1", skip={"rm:2"}), ["rm:3", "rm:4"])
        self.assertEqual(sorted(l.key for l in archive.pending("200", ["rm"], "s1", 10)), ["rm:1", "rm:2", "rm:3", "rm:4"])
        self.assertEqual(len(archive.pending("100", ["rm"], "s1", 1)), 1)       # limit
        self.assertEqual(archive.pending("100", ["otmr"], "s1", 10), [])

    def test_matched_listings_include_capped_alerts(self):
        archive.upsert([advert(1), advert(2), advert(3)], "head")
        archive.record_verdicts("100", [("rm:1", "matched", 1, 1, 1, 1), ("rm:2", "unsent", 1, 1, 1, 1),
                                        ("rm:3", "travel", 1, 1, 1, 1)], "s")
        self.assertEqual([l.key for l in archive.matched_listings("100", ["rm"])], ["rm:1", "rm:2"])
        self.assertEqual([l.key for l in archive.matched_listings("100", ["rm"], exclude={"rm:1"})], ["rm:2"])


class GoneTests(ArchiveBase):
    def sweep(self, listings, hour, complete=True):
        sid = archive.start_sweep("rm", SCOPE, {}, at(hour))
        archive.upsert(listings, "sweep", sid, at(hour))
        return archive.finish_sweep(sid, complete, "" if complete else "stopped", at(hour + 0.5))

    def test_missing_from_two_consecutive_complete_sweeps_means_gone(self):
        a, b = advert(1), advert(2)
        self.assertEqual(self.sweep([a, b], 0), [])
        self.assertEqual(self.sweep([a], 12), [])                  # first miss
        self.assertEqual(archive.advert("rm:2")["status"], "active")
        self.assertEqual(self.sweep([a], 24), ["rm:2"])            # second miss
        self.assertEqual(archive.advert("rm:2")["status"], "gone")
        self.assertEqual(archive.history("rm:2")[-1]["status"], "gone")
        self.assertEqual(archive.advert("rm:1")["status"], "active")

    def test_an_incomplete_sweep_never_marks_anything_gone_or_counts_a_miss(self):
        a, b = advert(1), advert(2)
        self.sweep([a, b], 0)
        self.assertEqual(self.sweep([a], 12, complete=False), [])
        self.assertEqual(self.sweep([a], 24, complete=False), [])
        self.assertEqual(self.sweep([a], 36, complete=False), [])
        self.assertEqual(archive.advert("rm:2")["status"], "active")
        self.assertEqual(self.sweep([a], 48), [])                  # only one complete miss so far
        self.assertEqual(self.sweep([a], 60), ["rm:2"])

    def test_a_sighting_in_between_resets_the_count(self):
        a, b = advert(1), advert(2)
        self.sweep([a, b], 0)
        self.sweep([a], 12)                                        # miss 1
        archive.upsert([b], "head", at=at(13))                     # the head poll still shows it
        self.assertEqual(self.sweep([a], 24), [])                  # miss 1 again, not 2
        self.assertEqual(archive.advert("rm:2")["missed"], 1)

    def test_head_poll_sighting_during_a_sweep_counts_as_present(self):
        a, b = advert(1), advert(2)
        self.sweep([a, b], 0)
        sid = archive.start_sweep("rm", SCOPE, {}, at(12))
        archive.upsert([a], "sweep", sid, at(12))
        archive.upsert([b], "head", at=at(12.2))                    # shown while the sweep ran, not by it
        archive.finish_sweep(sid, True, "", at(12.5))
        self.assertEqual(archive.advert("rm:2")["missed"], 0)

    def test_adverts_outside_the_swept_scope_are_left_alone(self):
        inside, dear, far, other = advert(1), advert(2, 2500), advert(3, lat=51.9), advert(4, source="otmr")
        self.sweep([inside], 0)
        archive.upsert([dear, far, other], "head", at=at(1))
        self.sweep([inside], 12)
        self.sweep([inside], 24)
        for key in ("rm:2", "rm:3", "otmr:4"):
            self.assertEqual(archive.advert(key)["status"], "active", key)

    def test_a_gone_advert_that_reappears_is_active_again_with_an_observation(self):
        a, b = advert(1), advert(2)
        self.sweep([a, b], 0)
        self.sweep([a], 12)
        self.sweep([a], 24)
        archive.upsert([b], "head", at=at(30))
        self.assertEqual(archive.advert("rm:2")["status"], "active")
        self.assertEqual([h["status"] for h in archive.history("rm:2")], ["active", "gone", "active"])


if __name__ == "__main__":
    unittest.main()
