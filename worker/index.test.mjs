import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import worker, { HELP } from "./src/index.js";

// D1's prepare/bind/first/all/run/batch API backed by node:sqlite and the real schema.
function d1() {
  const db = new DatabaseSync(":memory:");
  db.exec(readFileSync(new URL("./schema.sql", import.meta.url), "utf8"));
  const statement = (sql, params = []) => ({
    bind: (...values) => statement(sql, values),
    first: async () => db.prepare(sql).get(...params) ?? null,
    all: async () => ({ results: db.prepare(sql).all(...params) }),
    run: async () => db.prepare(sql).run(...params),
  });
  return {
    sqlite: db,
    prepare: (sql) => statement(sql),
    batch: async (statements) => Promise.all(statements.map((s) => s.run())),
  };
}

function setup() {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, body: JSON.parse(init.body) });
    return new Response(null, { status: url.includes("api.github.com") ? 204 : 200 });
  };
  const DB = d1();
  DB.sqlite.prepare("INSERT INTO listings VALUES (?, ?, ?, ?, ?, ?, ?)")
    .run("aaa", "stb:1", 51.5, -0.1, "https://example.com/1", "Home 1", 1001);
  const env = {
    TELEGRAM_BOT_TOKEN: "token", TELEGRAM_CHAT_ID: "42", TELEGRAM_WEBHOOK_SECRET: "secret",
    GITHUB_TOKEN: "gh", GITHUB_REPO: "HenryCorn/PisoBot", GITHUB_WORKFLOW: "pisobot.yml", GITHUB_REF: "main", DB,
  };
  return { env, calls, db: DB.sqlite };
}

const post = (update, secret = "secret") => new Request("https://worker.example/", {
  method: "POST", headers: { "X-Telegram-Bot-Api-Secret-Token": secret }, body: JSON.stringify(update),
});

const keyboard = [
  [{ text: "❤️ Save", callback_data: "s:aaa" }, { text: "👎 Hide", callback_data: "h:aaa" },
   { text: "🔕 Hide building", callback_data: "d:aaa" }],
  [{ text: "Save other", callback_data: "s:bbb" }],
  [{ text: "🔗 Open listing", url: "https://example.com/1" }],
];
const tap = (data) => post({ callback_query: {
  id: "q", data, message: { message_id: 5, chat: { id: 42 }, reply_markup: { inline_keyboard: keyboard } },
} });

test("rejects requests without the webhook secret", async () => {
  const { env, calls } = setup();
  assert.equal((await worker.fetch(post({}, "wrong"), env)).status, 403);
  assert.equal(calls.length, 0);
});

test("save stores the listing, un-hides it, and only relabels this listing's button", async () => {
  const { env, calls, db } = setup();
  db.prepare("INSERT INTO hidden VALUES ('stb:1')").run();
  await worker.fetch(tap("s:aaa"), env);
  const saved = db.prepare("SELECT key, info FROM saved").all();
  assert.deepEqual(saved.map((r) => r.key), ["stb:1"]);
  assert.deepEqual(JSON.parse(saved[0].info), {
    lat: 51.5, lng: -0.1, url: "https://example.com/1", address: "Home 1", cost: 1001, missing: 0,
  });
  assert.equal(db.prepare("SELECT COUNT(*) AS n FROM hidden").get().n, 0);
  const rows = calls.find((c) => c.url.endsWith("editMessageReplyMarkup")).body.reply_markup.inline_keyboard;
  assert.deepEqual(rows[0].map((b) => b.text), ["❤️ Saved", "👎 Hide", "🔕 Hide building"]);
  assert.deepEqual(rows.slice(1), keyboard.slice(1));
});

test("hide removes a saved home; hide building stores its location", async () => {
  const { env, db } = setup();
  await worker.fetch(tap("s:aaa"), env);
  await worker.fetch(tap("h:aaa"), env);
  await worker.fetch(tap("d:aaa"), env);
  assert.equal(db.prepare("SELECT COUNT(*) AS n FROM saved").get().n, 0);
  assert.deepEqual(db.prepare("SELECT key FROM hidden").all().map((r) => r.key), ["stb:1"]);
  assert.deepEqual({ ...db.prepare("SELECT lat, lng FROM hidden_dev").get() }, { lat: 51.5, lng: -0.1 });
});

test("unknown listings and other chats change nothing", async () => {
  const { env, calls, db } = setup();
  await worker.fetch(tap("s:zzz"), env);
  assert.equal(calls[0].body.text, "Listing no longer tracked");
  await worker.fetch(post({ callback_query: { id: "q", data: "s:aaa", message: { message_id: 1, chat: { id: 9 } } } }), env);
  assert.equal(db.prepare("SELECT COUNT(*) AS n FROM saved").get().n, 0);
});

test("/report dispatches the workflow with report=true", async () => {
  const { env, calls } = setup();
  await worker.fetch(post({ message: { chat: { id: 42 }, text: "/report@PisoBot" } }), env);
  const dispatch = calls.find((c) => c.url.includes("api.github.com"));
  assert.match(dispatch.url, /HenryCorn\/PisoBot\/actions\/workflows\/pisobot\.yml\/dispatches$/);
  assert.deepEqual(dispatch.body, { ref: "main", inputs: { report: "true" } });
  assert.match(calls.at(-1).body.text, /Report requested/);
});

test("/saved lists escaped homes and /unhide clears hidden choices", async () => {
  const { env, calls, db } = setup();
  db.prepare("INSERT INTO saved VALUES ('k', ?)").run(JSON.stringify({ url: "https://x/?a=1&b=2", address: "<Court>", cost: 1234.4 }));
  db.prepare("INSERT INTO hidden VALUES ('k')").run();
  db.prepare("INSERT INTO hidden_dev VALUES (51, 0)").run();
  await worker.fetch(post({ message: { chat: { id: 42 }, text: "/saved" } }), env);
  assert.match(calls.at(-1).body.text, /❤️ £1,234 · <a href="https:\/\/x\/\?a=1&amp;b=2">&lt;Court&gt;<\/a>/);
  await worker.fetch(post({ message: { chat: { id: 42 }, text: "/unhide" } }), env);
  assert.equal(db.prepare("SELECT (SELECT COUNT(*) FROM hidden) + (SELECT COUNT(*) FROM hidden_dev) AS n").get().n, 0);
});

test("help keeps the legend", () => {
  assert.match(HELP, /M = modelled mortgage/);
  assert.match(HELP, /\/report/);
});
