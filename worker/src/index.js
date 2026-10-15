// Telegram webhook for PisoBot. Buttons and commands are applied to D1 immediately; the
// GitHub Actions run reads those choices, and /report dispatches that run straight away.

export const HELP =
  "<b>PisoBot</b>\n" +
  "Buttons: ❤️ Save · 👎 Hide · 🔕 Hide building. They apply instantly; " +
  "hidden homes drop out of the next report.\n\n" +
  "🆕 New since the last report · ❤️ Saved · ⭐ Deposit at most £10k.\n" +
  "M = modelled mortgage · R = rent · SC = service charge. " +
  "~ / est. means an estimated input, not a published cost. " +
  "Mortgage uses the configured repayment rate/term, not a lender's offer.\n" +
  "⚠️ High SC: over £250/mo. Lease under 90 years: confirm extension costs with a solicitor.\n" +
  "Crime: Low/Med/High are thirds of the latest month's sampled London search locations " +
  "(minimum 20 distinct ~100m locations), not official London-wide percentiles " +
  "or population-adjusted risk. Unrated means there is insufficient reference data.\n" +
  "Header: A = Oxford Circus; B = source listing pin (may be approximate). " +
  "North-up straight line, not a transit route. Map: © OpenStreetMap contributors.\n" +
  "Reports arrive at 06:30 and 18:00 UK time; zero-new reports are a silent summary.\n\n" +
  "/report – full report now (arrives in a few minutes)\n" +
  "/saved – list saved homes\n" +
  "/unhide – show hidden homes again\n" +
  "/help – this message";

const DEFAULT_LABELS = { s: "❤️ Save", h: "👎 Hide", d: "🔕 Hide building" };

function escapeHtml(text) {
  return String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}

async function telegram(env, method, body) {
  const response = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) console.error(method, response.status, await response.text());
}

function reply(env, text) {
  return telegram(env, "sendMessage", {
    chat_id: env.TELEGRAM_CHAT_ID,
    text,
    parse_mode: "HTML",
    disable_web_page_preview: true,
  });
}

function fromOwnChat(chat, env) {
  return chat && String(chat.id) === String(env.TELEGRAM_CHAT_ID);
}

// Mirrors the old prefs.process_updates: Save un-hides, Hide un-saves, Hide building hides nearby plots.
function statements(env, action, listing) {
  const info = JSON.stringify({
    lat: listing.lat, lng: listing.lng, url: listing.url, address: listing.address, cost: listing.cost, missing: 0,
  });
  if (action === "s") {
    return [
      env.DB.prepare("INSERT INTO saved (key, info) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET info = excluded.info")
        .bind(listing.key, info),
      env.DB.prepare("DELETE FROM hidden WHERE key = ?").bind(listing.key),
    ];
  }
  if (action === "h") {
    return [
      env.DB.prepare("INSERT OR IGNORE INTO hidden (key) VALUES (?)").bind(listing.key),
      env.DB.prepare("DELETE FROM saved WHERE key = ?").bind(listing.key),
    ];
  }
  return [env.DB.prepare("INSERT OR IGNORE INTO hidden_dev (lat, lng) VALUES (?, ?)").bind(listing.lat, listing.lng)];
}

async function handleCallback(query, env) {
  const [action, sid] = (query.data || "").split(":");
  const message = query.message;
  if (!(action in DEFAULT_LABELS) || !sid || !fromOwnChat(message?.chat, env)) {
    await telegram(env, "answerCallbackQuery", { callback_query_id: query.id });
    return;
  }
  const listing = await env.DB.prepare("SELECT * FROM listings WHERE sid = ?").bind(sid).first();
  if (!listing) {
    await telegram(env, "answerCallbackQuery", { callback_query_id: query.id, text: "Listing no longer tracked" });
    return;
  }
  await env.DB.batch(statements(env, action, listing));
  const label = { s: "❤️ Saved", h: "👎 Hidden", d: "🔕 Building hidden" }[action];
  // Only this listing's Save/Hide buttons change; other cards' buttons and links are kept.
  const keyboard = (message.reply_markup?.inline_keyboard || []).map((row) =>
    row.map((button) => {
      const [buttonAction, buttonSid] = (button.callback_data || "").split(":");
      if (buttonSid !== sid || !(buttonAction in DEFAULT_LABELS)) return button;
      return { ...button, text: buttonAction === action ? label : DEFAULT_LABELS[buttonAction] };
    }),
  );
  await Promise.all([
    telegram(env, "answerCallbackQuery", { callback_query_id: query.id, text: label }),
    telegram(env, "editMessageReplyMarkup", {
      chat_id: message.chat.id,
      message_id: message.message_id,
      reply_markup: { inline_keyboard: keyboard },
    }),
  ]);
}

async function dispatchReport(env) {
  const response = await fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${env.GITHUB_WORKFLOW}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pisobot-worker",
      },
      body: JSON.stringify({ ref: env.GITHUB_REF, inputs: { report: "true" } }),
    },
  );
  if (response.status === 204) return "📊 Report requested · it arrives in a few minutes.";
  console.error("dispatch", response.status, await response.text());
  return `⚠️ Couldn't start the report (GitHub ${response.status}). Try again or run the workflow manually.`;
}

async function handleCommand(text, env) {
  const command = (text.split(/\s+/)[0] || "").split("@")[0].toLowerCase();
  if (command === "/report") {
    await reply(env, await dispatchReport(env));
  } else if (command === "/saved") {
    const { results } = await env.DB.prepare("SELECT info FROM saved").all();
    const lines = results.map((row) => {
      const info = JSON.parse(row.info);
      const cost = `£${Math.round(info.cost || 0).toLocaleString("en-GB")}`;
      return `❤️ ${cost} · <a href="${escapeHtml(info.url)}">${escapeHtml((info.address || "listing").slice(0, 60))}</a>`;
    });
    await reply(env, "<b>Saved homes</b>\n" + (lines.join("\n") || "Nothing saved yet."));
  } else if (command === "/unhide") {
    await env.DB.batch([env.DB.prepare("DELETE FROM hidden"), env.DB.prepare("DELETE FROM hidden_dev")]);
    await reply(env, "👀 Hidden homes will show again from the next report.");
  } else if (command === "/help" || command === "/start") {
    await reply(env, HELP);
  }
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("PisoBot webhook");
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.TELEGRAM_WEBHOOK_SECRET) {
      return new Response("Forbidden", { status: 403 });
    }
    const update = await request.json();
    if (update.callback_query) {
      await handleCallback(update.callback_query, env);
    } else if (update.message?.text && fromOwnChat(update.message.chat, env)) {
      await handleCommand(update.message.text, env);
    }
    return new Response("ok");
  },
};
