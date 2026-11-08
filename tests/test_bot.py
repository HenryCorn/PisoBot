import copy
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import Mock, patch

import requests

from pisobot import bot, net


TOKEN = "fake-token-for-offline-tests"
CHAT = "-1234"
SID = "0123456789"
KEY = "portal:1"


def callback(data, markup=None, chat=CHAT):
    return {"callback_query": {
        "id": "callback-id", "data": data,
        "message": {"chat": {"id": chat}, "message_id": 41,
                    "reply_markup": copy.deepcopy(markup if markup is not None else keyboard())},
    }}


def message(text, chat=CHAT):
    return {"message": {"chat": {"id": chat}, "text": text}}


def keyboard():
    return {"inline_keyboard": [
        [{"text": bot.DEFAULT_LABELS[action], "callback_data": f"{action}:{SID}"}
         for action in ("s", "h", "d")],
        [{"text": "🔗 Open listing", "url": "https://example.com/a?x=1&y=2"}],
        [{"text": "Other home", "callback_data": "s:other"},
         {"text": "Other action", "callback_data": f"x:{SID}", "extra": "keep"}],
    ]}


def ok(result=True):
    return {"ok": True, "result": result}


class Stop:
    def __init__(self):
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.waits.append(seconds)
        if len(self.waits) > 5:
            self.set()  # A broken fake script fails assertions instead of hanging the suite.
        return self.stopped


class FakeHTTP:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def post(self, url, **kwargs):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, kwargs))
        if not self.script:
            raise AssertionError("Unexpected HTTP call")
        expected, response = self.script.pop(0)
        if method != expected:
            raise AssertionError(f"Expected {expected}, got {method}")
        if callable(response):
            response = response(kwargs["json"])
        if isinstance(response, Exception):
            raise response
        if isinstance(response, tuple):
            status, body = response
        else:
            status, body = 200, response
        return Mock(status_code=status, json=Mock(return_value=body))


class BotTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        schema = Path(__file__).resolve().parents[1] / "worker" / "schema.sql"
        self.db.executescript(schema.read_text())
        self.addCleanup(self.db.close)
        self.sql = patch.object(bot.d1, "query", side_effect=self.query).start()
        self.addCleanup(patch.stopall)
        # A missing mock must not turn a regression test into a production request.
        patch.object(net.session, "post", side_effect=AssertionError("Shared HTTP used")).start()
        self.query("INSERT INTO listings VALUES (?, ?, ?, ?, ?, ?, ?)",
                   SID, KEY, 51.5, -0.1, "https://example.com/?a=1&b=2", "A <home>", 1234)
        self.client = Mock()
        self.trigger = Mock(return_value=True)
        self.handler = bot.Handler(CHAT, self.trigger, self.client)

    def query(self, sql, *params):
        cursor = self.db.execute(sql, params)
        rows = [dict(row) for row in cursor.fetchall()]
        self.db.commit()
        return rows

    def markup(self):
        return next(call.kwargs["reply_markup"] for call in reversed(self.client.call.call_args_list)
                    if call.args[0] == "editMessageReplyMarkup")

    def checkpoint(self):
        rows = self.query("SELECT value FROM meta WHERE name = ?", bot.OFFSET_KEY)
        return rows[0]["value"] if rows else None

    def test_only_allowlisted_chat_including_callbacks_can_act(self):
        for update in (message("/report", 777), callback(f"s:{SID}", chat=777),
                       {"callback_query": {"id": "x", "data": f"s:{SID}", "from": {"id": CHAT}}},
                       message("/reset", True), message("/help", None)):
            self.handler.handle(update)
        self.client.call.assert_not_called()
        self.trigger.assert_not_called()
        self.sql.assert_not_called()

    def test_malformed_external_updates_are_ignored(self):
        for update in (None, [], "bad", {}, {"message": []}, {"message": {"text": []}},
                       {"callback_query": []}, {"callback_query": {"message": []}},
                       message(""), message("   "), message([]),
                       callback(None), callback(["s", SID]),
                       callback(f"s:{SID}", {"inline_keyboard": [None]}),
                       callback(f"s:{SID}", {"inline_keyboard": [[42]]}),
                       callback("s:" + "a" * 100)):
            self.handler.handle(update)
        self.client.call.assert_not_called()
        self.sql.assert_not_called()

    def test_commands_inject_run_trigger_and_coalesce(self):
        self.handler.handle(message("/REPORT@PisoBot more"))
        self.handler.handle(message("/rentals"))
        self.trigger.return_value = False
        self.handler.handle(message("/report"))
        self.assertEqual([c.args for c in self.trigger.call_args_list],
                         [("report",), ("rentals",), ("report",)])
        self.assertIn("already queued", self.client.call.call_args.kwargs["text"])
        for command in ("/help", "/start"):
            self.handler.handle(message(command))
            self.assertEqual(self.client.call.call_args.kwargs["text"], bot.HELP)
        self.assertIn("/unhide", bot.HELP)
        self.assertIn("/reset", bot.HELP)
        before = self.client.call.call_count
        self.handler.handle(message("/unknown"))
        self.assertEqual(self.client.call.call_count, before)

    def test_save_hide_and_building_preserve_exact_existing_buttons(self):
        original = keyboard()
        self.query("INSERT INTO hidden VALUES (?)", KEY)
        for action in ("s", "h", "d"):
            self.handler.handle(callback(f"{action}:{SID}", original))
            expected = copy.deepcopy(original)
            for button, candidate in zip(expected["inline_keyboard"][0], ("s", "h", "d")):
                button["text"] = (bot.ACTIVE_LABELS[action] if candidate == action
                                  else bot.DEFAULT_LABELS[candidate])
            if action in ("s", "h"):
                expected["inline_keyboard"].insert(1, [
                    {"text": bot.REASON_LABELS[reason], "callback_data": f"r:{action}:{reason}:{SID}"}
                    for reason in bot.REASONS[action]
                ])
            self.assertEqual(self.markup(), expected)
            self.assertEqual(original, keyboard())
            if action == "s":
                saved = json.loads(self.query("SELECT info FROM saved")[0]["info"])
                self.assertEqual(saved, {"lat": 51.5, "lng": -0.1,
                                        "url": "https://example.com/?a=1&b=2",
                                        "address": "A <home>", "cost": 1234, "missing": 0})
                self.assertEqual(self.query("SELECT * FROM hidden"), [])
            elif action == "h":
                self.assertEqual(self.query("SELECT * FROM saved"), [])
                self.assertEqual(self.query("SELECT * FROM hidden"), [{"key": KEY}])
            else:
                self.assertEqual(self.query("SELECT * FROM hidden_dev"), [{"lat": 51.5, "lng": -0.1}])
        self.handler.handle(callback(f"s:{SID}"))
        self.assertEqual(self.query("SELECT * FROM hidden"), [])
        self.assertEqual(len(self.query("SELECT * FROM hidden_dev")), 1)

    def test_reasons_feedback_and_ack_preserve_other_buttons(self):
        self.handler.handle(callback(f"s:{SID}"))
        markup = self.markup()
        markup["inline_keyboard"][1].append({"text": "Keep me", "url": "https://example.com"})
        update = callback(f"r:s:area:{SID}", markup)
        self.handler.handle(update)
        self.handler.handle(update)
        self.assertEqual(self.query("SELECT key, action, reason FROM feedback"),
                         [{"key": KEY, "action": "s", "reason": "area"}])
        rows = self.markup()["inline_keyboard"]
        self.assertEqual(rows[1], [{"text": "Keep me", "url": "https://example.com"}])
        self.assertEqual(rows[2], [{"text": "✓ Noted: 📍 Area", "callback_data": f"r:ack:{SID}"}])
        self.assertEqual(rows[0], markup["inline_keyboard"][0])
        self.assertEqual(rows[3:], markup["inline_keyboard"][2:])
        self.handler.handle(callback(f"h:{SID}", self.markup()))
        self.assertEqual(self.markup()["inline_keyboard"][2][0]["callback_data"],
                         f"r:h:price:{SID}")
        self.sql.reset_mock()
        self.handler.handle(callback(f"r:ack:{SID}"))
        self.sql.assert_not_called()
        self.assertEqual(self.client.call.call_args.args[0], "answerCallbackQuery")

    def test_existing_reason_row_replaced_not_duplicated_and_removed_for_building(self):
        original = keyboard()
        original["inline_keyboard"].insert(2, [
            {"text": "Reason", "callback_data": f"r:s:area:{SID}"},
            {"text": "Other home's reason", "callback_data": "r:s:area:other"},
        ])
        self.handler.handle(callback(f"h:{SID}", original))
        rows = self.markup()["inline_keyboard"]
        self.assertEqual(rows[1], original["inline_keyboard"][1])
        self.assertEqual(rows[2], [original["inline_keyboard"][2][1]])
        self.assertEqual(len([b for row in rows for b in row
                              if b.get("callback_data", "").startswith("r:h:")]), 4)
        self.handler.handle(callback(f"d:{SID}", self.markup()))
        self.assertFalse(any(b.get("callback_data", "").endswith(":" + SID)
                             and b.get("callback_data", "").startswith("r:")
                             for row in self.markup()["inline_keyboard"] for b in row))
        self.assertIn([original["inline_keyboard"][2][1]], self.markup()["inline_keyboard"])

    def test_unknown_listing_invalid_reason_and_missing_coordinates(self):
        for data in ("s:missing", f"r:d:area:{SID}", f"r:s:trap:{SID}", "s:", "unknown"):
            self.handler.handle(callback(data))
        self.assertEqual(self.query("SELECT * FROM feedback"), [])
        self.assertEqual(self.query("SELECT * FROM saved"), [])
        self.query("UPDATE listings SET lat = NULL")
        self.handler.handle(callback(f"d:{SID}"))
        self.assertEqual(self.query("SELECT * FROM hidden_dev"), [])
        self.assertEqual(self.client.call.call_args.kwargs["text"], "Building location unavailable")

    def test_saved_list_escapes_html_and_unhide_keeps_saved(self):
        self.handler.handle(message("/saved"))
        self.assertIn("Nothing saved yet", self.client.call.call_args.kwargs["text"])
        self.handler.handle(callback(f"s:{SID}"))
        self.handler.handle(message("/saved"))
        text = self.client.call.call_args.kwargs["text"]
        self.assertIn("£1,234", text)
        self.assertIn("A &lt;home&gt;", text)
        self.assertIn("a=1&amp;b=2", text)
        self.query("INSERT INTO hidden VALUES ('other')")
        self.query("INSERT INTO hidden_dev VALUES (1, 2)")
        self.handler.handle(message("/unhide"))
        self.assertEqual(self.query("SELECT * FROM hidden"), [])
        self.assertEqual(self.query("SELECT * FROM hidden_dev"), [])
        self.assertEqual(len(self.query("SELECT * FROM saved")), 1)

    def test_restart_and_reset_keep_transport_and_scheduler_state(self):
        scheduler_state = {
            "watch_initialized": "1",
            "watch_failures": "2",
            "watch_warned": "1",
            "watch_last_success": "2026-11-08T19:00:00+00:00",
            "future_scheduler_key": "preserve",
        }
        for name, value in scheduler_state.items():
            self.query("INSERT INTO meta VALUES (?, ?)", name, value)
        for command in ("/restart", "/reset"):
            self.handler.handle(callback(f"s:{SID}"))
            self.query("INSERT OR IGNORE INTO hidden VALUES ('other')")
            self.query("INSERT OR IGNORE INTO hidden_dev VALUES (1, 2)")
            self.query("INSERT OR REPLACE INTO meta VALUES (?, '50')", bot.OFFSET_KEY)
            self.query("INSERT OR REPLACE INTO meta VALUES ('last_report', 'yesterday')")
            self.query("INSERT OR REPLACE INTO feedback VALUES (?, 's', 'area', 'today')", KEY)
            self.handler.handle(message(command))
            self.assertEqual(self.checkpoint(), "50")
            self.assertEqual(
                {row["name"]: row["value"] for row in self.query(
                    "SELECT name, value FROM meta WHERE name != ?", bot.OFFSET_KEY)},
                scheduler_state,
            )
            self.assertEqual(self.query("SELECT * FROM meta WHERE name='last_report'"), [])
            self.assertEqual(self.query("SELECT * FROM hidden"), [])
            self.assertEqual(self.query("SELECT * FROM hidden_dev"), [])
            self.assertEqual(len(self.query("SELECT * FROM saved")), int(command == "/restart"))
            self.assertEqual(len(self.query("SELECT * FROM feedback")), int(command == "/restart"))
            self.trigger.assert_called_with("report")
            if command == "/reset":
                self.assertIn("Listing history and scheduler state kept",
                              self.client.call.call_args.kwargs["text"])
        self.assertIn("keeps listing history", bot.HELP)
        self.assertNotIn("factory reset", bot.HELP)

    def test_expired_acknowledgements_do_not_poison_updates(self):
        def call(method, **kwargs):
            if method == "answerCallbackQuery":
                raise bot.TelegramError("Telegram request failed")
        self.client.call.side_effect = call
        with self.assertLogs(bot.LOG, level="WARNING"):
            self.handler.handle(callback(f"s:{SID}"))
            self.handler.handle(callback("s:missing"))
            self.handler.handle(callback(f"r:ack:{SID}"))
        self.assertEqual(len(self.query("SELECT * FROM saved")), 1)

    def test_missing_keyboard_message_keeps_durable_choice(self):
        def call(method, **kwargs):
            if method == "editMessageReplyMarkup":
                raise bot.TelegramError("Telegram request failed", unavailable_message=True)
        self.client.call.side_effect = call
        with self.assertLogs(bot.LOG, level="WARNING"):
            self.handler.handle(callback(f"s:{SID}"))
        self.assertEqual(len(self.query("SELECT * FROM saved")), 1)

    def poll(self, script, stop=None):
        stop = stop or Stop()
        http = FakeHTTP(script)
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT}), \
                patch.object(bot.requests, "Session", return_value=http):
            bot.poll(self.trigger, stop)
        self.assertTrue(http.closed)
        self.assertEqual(http.script, [])
        for _, kwargs in http.calls:
            self.assertLessEqual(kwargs["timeout"], 35)
        return http, stop

    def handshake(self):
        return [("deleteWebhook", ok()), ("getWebhookInfo", ok({"url": ""}))]

    def finish(self, stop):
        def finish(body):
            stop.set()
            return ok([])
        return ("getUpdates", finish)

    def test_webhook_failure_refuses_to_poll_and_never_drops_pending(self):
        scripts = [
            [("deleteWebhook", ok(False))],
            [("deleteWebhook", {"ok": False})],
            [("deleteWebhook", requests.ConnectionError("failed " + TOKEN))],
            [("deleteWebhook", ok()), ("getWebhookInfo", ok({"url": "https://old.example"}))],
            [("deleteWebhook", ok()), ("getWebhookInfo", ok({}))],
            [("deleteWebhook", ok()), ("getWebhookInfo", {"ok": False})],
        ]
        for script in scripts:
            with self.subTest(script=script):
                http = FakeHTTP(script)
                with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT}), \
                        patch.object(bot.requests, "Session", return_value=http), \
                        self.assertRaises(bot.TelegramError) as raised:
                    bot.poll(self.trigger, Stop())
                self.assertNotIn(TOKEN, str(raised.exception))
                self.assertTrue(http.closed)
                self.assertNotIn("getUpdates", [m for m, _ in http.calls])
                self.assertEqual(http.calls[0][1]["json"], {})
                self.assertTrue(all("drop_pending_updates" not in k["json"] for _, k in http.calls))
        self.sql.assert_not_called()

    def test_durable_offsets_resume_and_skip_unauthorized_and_malformed_updates(self):
        self.query("INSERT INTO meta VALUES (?, '10')", bot.OFFSET_KEY)
        stop = Stop()
        updates = [
            {"update_id": 9, **message("/report")},
            {"update_id": 10, **message("/report", "stranger")},
            {"update_id": 11, **callback(f"s:{SID}", chat="stranger")},
            {"update_id": 12, "callback_query": []},
            {"update_id": 13, **message("/report")},
            {"update_id": 13, **message("/report")},
            {"update_id": 14, **callback(f"s:{SID}")},
        ]

        def finish(body):
            self.assertEqual(self.checkpoint(), "15")
            stop.set()
            return ok([])

        http, _ = self.poll(self.handshake() + [
            ("getUpdates", ok(updates)), ("sendMessage", ok()),
            ("editMessageReplyMarkup", ok()), ("answerCallbackQuery", ok()),
            ("getUpdates", finish),
        ], stop)
        polls = [kw["json"] for m, kw in http.calls if m == "getUpdates"]
        self.assertEqual([p["offset"] for p in polls], [10, 15])
        self.assertEqual(polls[0]["allowed_updates"], ["message", "callback_query"])
        self.assertLess(polls[0]["timeout"], bot.HTTP_TIMEOUT)
        self.trigger.assert_called_once_with("report")
        self.assertEqual(len(self.query("SELECT * FROM saved")), 1)
        self.assertEqual(stop.waits, [])

    def test_handler_failure_retries_without_acknowledging_later_updates(self):
        self.trigger.side_effect = [RuntimeError("secret " + TOKEN), True, True]
        stop = Stop()
        updates = [{"update_id": 1, **message("/report")}, {"update_id": 2, **message("/rentals")}]
        with self.assertLogs(bot.LOG, level="WARNING") as logs:
            http, _ = self.poll(self.handshake() + [
                ("getUpdates", ok(updates)), ("getUpdates", ok(updates)),
                ("sendMessage", ok()), ("sendMessage", ok()), self.finish(stop),
            ], stop)
        self.assertEqual([kw["json"]["offset"] for m, kw in http.calls if m == "getUpdates"],
                         [0, 0, 3])
        self.assertEqual([c.args for c in self.trigger.call_args_list],
                         [("report",), ("report",), ("rentals",)])
        self.assertEqual(self.checkpoint(), "3")
        self.assertEqual(stop.waits, [bot.RETRY_SECONDS])
        self.assertNotIn(TOKEN, "\n".join(logs.output))

    def test_checkpoint_failure_replays_idempotent_save(self):
        fail = True

        def query(sql, *params):
            nonlocal fail
            if sql.startswith("INSERT INTO meta") and fail:
                fail = False
                raise RuntimeError("DB unavailable")
            return self.query(sql, *params)

        self.sql.side_effect = query
        stop = Stop()
        updates = [{"update_id": 7, **callback(f"s:{SID}")}]
        with self.assertLogs(bot.LOG, level="WARNING"):
            http, _ = self.poll(self.handshake() + [
                ("getUpdates", ok(updates)), ("editMessageReplyMarkup", ok()),
                ("answerCallbackQuery", ok()), ("getUpdates", ok(updates)),
                ("editMessageReplyMarkup", (400, {"ok": False, "error_code": 400,
                 "description": "Bad Request: message is not modified: same markup"})),
                ("answerCallbackQuery", ok()), self.finish(stop),
            ], stop)
        self.assertEqual([kw["json"]["offset"] for m, kw in http.calls if m == "getUpdates"], [0, 0, 8])
        self.assertEqual(self.checkpoint(), "8")
        self.assertEqual(len(self.query("SELECT * FROM saved")), 1)

    def test_partial_db_failure_retries_before_checkpointing(self):
        fail = True

        def query(sql, *params):
            nonlocal fail
            if sql.startswith("DELETE FROM hidden WHERE") and fail:
                fail = False
                raise RuntimeError("DB unavailable")
            return self.query(sql, *params)

        self.sql.side_effect = query
        stop = Stop()
        updates = [{"update_id": 7, **callback(f"s:{SID}")}]
        with self.assertLogs(bot.LOG, level="WARNING"):
            self.poll(self.handshake() + [
                ("getUpdates", ok(updates)), ("getUpdates", ok(updates)),
                ("editMessageReplyMarkup", ok()), ("answerCallbackQuery", ok()), self.finish(stop),
            ], stop)
        self.assertEqual(self.checkpoint(), "8")
        self.assertEqual(len(self.query("SELECT * FROM saved")), 1)

    def test_keyboard_network_failure_does_not_checkpoint_the_update(self):
        stop = Stop()
        updates = [{"update_id": 7, **callback(f"s:{SID}")}]
        with self.assertLogs(bot.LOG, level="WARNING"):
            http, _ = self.poll(self.handshake() + [
                ("getUpdates", ok(updates)),
                ("editMessageReplyMarkup", requests.Timeout("failed " + TOKEN)),
                ("getUpdates", ok(updates)), ("editMessageReplyMarkup", ok()),
                ("answerCallbackQuery", ok()), self.finish(stop),
            ], stop)
        self.assertEqual([kw["json"]["offset"] for m, kw in http.calls if m == "getUpdates"], [0, 0, 8])
        self.assertEqual(self.checkpoint(), "8")
        self.assertEqual(len(self.query("SELECT * FROM saved")), 1)

    def test_network_and_api_errors_back_off_without_exposing_token(self):
        for failure in (requests.Timeout("https://api.telegram.org/bot" + TOKEN),
                        {"ok": False, "description": TOKEN}, ["invalid response"], ok({}),
                        (503, ok([]))):
            with self.subTest(failure=failure), self.assertLogs(bot.LOG, level="WARNING") as logs:
                stop = Stop()
                http, _ = self.poll(self.handshake() + [
                    ("getUpdates", failure), self.finish(stop),
                ], stop)
                self.assertEqual(stop.waits, [bot.RETRY_SECONDS])
                self.assertNotIn(TOKEN, "\n".join(logs.output))
                self.assertEqual(self.checkpoint(), None)

    def test_stop_during_poll_and_already_stopped_do_not_process_updates(self):
        stop = Stop()

        def response(body):
            stop.set()
            return ok([{"update_id": 1, **message("/reset")}])

        self.poll(self.handshake() + [("getUpdates", response)], stop)
        self.trigger.assert_not_called()
        self.assertIsNone(self.checkpoint())
        with patch.object(bot.requests, "Session") as session:
            bot.poll(self.trigger, stop)
        session.assert_not_called()

    def test_stop_interrupts_error_backoff_and_closes_session(self):
        stop = Stop()
        stop.wait = Mock(side_effect=lambda seconds: stop.set())
        with self.assertLogs(bot.LOG, level="WARNING"):
            self.poll(self.handshake() + [
                ("getUpdates", requests.ConnectionError("failed " + TOKEN)),
            ], stop)
        stop.wait.assert_called_once_with(bot.RETRY_SECONDS)
        self.assertIsNone(self.checkpoint())

    def test_bad_checkpoint_fails_closed_instead_of_replaying_history(self):
        self.query("INSERT INTO meta VALUES (?, 'not-a-number')", bot.OFFSET_KEY)
        with self.assertRaisesRegex(RuntimeError, "Invalid Telegram update checkpoint"):
            self.poll(self.handshake())

    def test_telegram_client_sanitizes_errors_and_validates_ok(self):
        for response in ([1], {"ok": 1, "result": True}, (403, {"ok": False, "description": TOKEN}),
                         requests.ConnectionError("failed " + TOKEN)):
            http = FakeHTTP([("sendMessage", response)])
            with self.assertRaises(bot.TelegramError) as raised:
                bot.TelegramClient(TOKEN, http).call("sendMessage", text="test")
            self.assertNotIn(TOKEN, str(raised.exception))
        http = FakeHTTP([("editMessageReplyMarkup", (400, {
            "ok": False, "error_code": 400, "description": "Bad Request: message to edit not found",
        }))])
        with self.assertRaises(bot.TelegramError) as raised:
            bot.TelegramClient(TOKEN, http).call("editMessageReplyMarkup")
        self.assertTrue(raised.exception.unavailable_message)


if __name__ == "__main__":
    unittest.main()
