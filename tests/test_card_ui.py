import io
import unittest
from unittest.mock import patch

from pisobot import cache, enrich, main, prefs, telegram
from pisobot.models import Listing


def home():
    return Listing("stb", "1", "https://example.com/1", "Chaseway Lodge, London, E16 2LS", 1, "SO Resale",
                   share_pct=25, share_price=67500, full_value=270000, deposit=10000,
                   mortgage=310, rent=481, service_charge=230, monthly_cost=1021,
                   floor_area_m2=51, lease_years=112, travel_minutes=31, walk_minutes=12,
                   station="Custom House", route="Elizabeth line → Windrush", crime_score=41,
                   crime_percentile=50, lat=51.5, lng=0.02,
                   description="Private balcony. Valid EWS1. Pets are not allowed.")


class CardTests(unittest.TestCase):
    def test_target_card_exposes_financials_location_walk_area_and_risks(self):
        text = telegram.format_listing(home(), rank=1)
        for part in ("#1 · Canning Town / Custom House (E16)", "Chaseway Lodge", "£1,021/mo",
                     "M: £310 · R: £481 · SC: £230", "25% of £270k", "£67.5k", "£10k dep",
                     "51 m²", "£5,294/m²", "112y lease", "🚶12m Custom House", "Oxford Circus",
                     "🟡 Med (41)", "🌿 Balcony", "✅ EWS1", "⚠️ Pets not allowed"):
            self.assertIn(part, text)
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 1024)
        self.assertEqual(len(text.splitlines()), 6)

    def test_unknown_facts_and_negative_features_are_not_invented(self):
        l = home()
        l.description = "No balcony. No parking. Lift is not available. EWS1 required. Cladding inspected."
        self.assertEqual(l.feature_tags, ["⚠️ Cladding mentioned"])
        l.description = "No valid EWS1 available."
        self.assertNotIn("✅ EWS1", l.feature_tags)
        l.description = "Valid EWS1 B2 certificate."
        self.assertNotIn("✅ EWS1", l.feature_tags)
        self.assertIn("⚠️ EWS1 B2", l.feature_tags)
        l.lease_years = None
        l.crime_percentile = None
        self.assertIn("📜 Lease: TBC", telegram.format_listing(l))
        self.assertIn("⚪ Unrated (41)", telegram.format_listing(l))
        l.lease_years, l.service_charge = 89, 251
        self.assertIn("89y lease</b> ⚠️", telegram.format_listing(l))
        self.assertIn("⚠️ High SC", telegram.format_listing(l))

    def test_crime_colours_use_current_month_reference_not_only_accepted_homes(self):
        data = {f"51.{500+i},-0.100:2026-08:500": i for i in range(30)}
        data["51.500,-0.100:2026-07:500"] = 100000
        listings = [home() for _ in range(3)]
        for l, score in zip(listings, (1, 15, 29)):
            l.crime_score = score
        with patch.object(cache, "_data", {"crime": data}), \
                patch.object(enrich, "_crime_month", return_value="2026-08"):
            enrich.assign_crime_percentiles(listings)
        self.assertTrue(telegram.crime_label(listings[0]).startswith("🟢 Low"))
        self.assertTrue(telegram.crime_label(listings[1]).startswith("🟡 Med"))
        self.assertTrue(telegram.crime_label(listings[2]).startswith("🔴 High"))
        with patch.object(cache, "_data", {}), patch.object(enrich, "_crime_month", return_value="2026-08"):
            enrich.assign_crime_percentiles(listings)
        self.assertTrue(all(l.crime_percentile is None for l in listings))

    def test_individual_alerts_use_the_same_composite_and_card_template(self):
        with patch.object(telegram.collage, "build", return_value=b"jpeg") as build, \
                patch.object(telegram, "send_photo_bytes") as photo:
            telegram.send_listing(home())
        build.assert_called_once()
        self.assertIn("M: £310", photo.call_args.args[1])
        self.assertEqual(photo.call_args.args[2], telegram.listing_keyboard(home()))

    def test_summary_legend_moves_to_help_and_silence_is_sent_to_telegram(self):
        title = telegram.report_messages([home()], [], [], "Evening report", lambda l: "")[0][0]
        self.assertNotIn("checked every", title)
        self.assertNotIn("New since", title)
        with patch.object(telegram, "_call") as call:
            telegram.send_text("0 new homes", silent=True)
        self.assertEqual(call.call_args.kwargs["disable_notification"], "true")

    def test_scheduled_zero_new_sends_one_silent_summary_but_manual_report_is_full(self):
        for scheduled, old_cost in ((True, 1021), (False, 1021), (True, 1071)):
            l, other = home(), home()
            other.id, other.lng = "2", -0.12
            seen = {item.key: {"alerted": True, "cost": cost, "first": "2026-01-01T06:30"}
                    for item, cost in ((l, old_cost), (other, 1021))}
            p = {"saved": {}, "hidden": [], "hidden_dev": [], "last_report": "2026-01-02T18:00"}
            with patch.object(main, "load_seen", return_value=seen), \
                    patch.object(prefs, "load", return_value=p), patch.object(prefs, "publish"), \
                    patch.object(main.SOURCES["stb"], "fetch", return_value=[l, other]), \
                    patch.object(enrich, "geocode"), patch.object(enrich, "assign_crime_percentiles"), \
                    patch.object(main, "passes_basic", return_value=None), \
                    patch.object(main, "passes_area", return_value=None), patch.object(main.mapgen, "write"), \
                    patch.object(telegram, "send_text") as send, patch.object(main, "send_report") as report, \
                    patch.object(cache, "save"), patch.object(main, "save_seen"), patch.object(prefs, "save"), \
                    patch("sys.stdout", new=io.StringIO()):
                main.run(["stb"], False, False, report=True, scheduled=scheduled)
            if scheduled and old_cost == 1021:
                report.assert_not_called()
                send.assert_called_once()
                self.assertIn("0 new homes · 2 tracked", send.call_args.args[0])
                self.assertTrue(send.call_args.kwargs["silent"])
            else:
                report.assert_called_once()
                if scheduled:
                    self.assertEqual(report.call_args.args[0], [l])
                    self.assertEqual(report.call_args.args[2], [])

    def test_all_source_failure_is_not_recorded_as_successful_report_delivery(self):
        p = {"last_report": "2026-01-01T18:00"}
        with patch.object(main, "load_seen", return_value={}), patch.object(prefs, "load", return_value=p), \
                patch.object(main.SOURCES["stb"], "fetch", side_effect=RuntimeError("source unavailable")), \
                patch.object(prefs, "save") as save, patch("sys.stderr", new=io.StringIO()), \
                self.assertRaisesRegex(RuntimeError, "Every source failed"):
            main.run(["stb"], False, False, report=True, scheduled=True)
        self.assertEqual(p["last_report"], "2026-01-01T18:00")
        save.assert_not_called()
