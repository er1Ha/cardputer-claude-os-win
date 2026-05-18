// Multi-provider usage cache.
//
// Host-side scripts (codex-usage, claude-usage) POST their latest
// rate-limit snapshot. The Cardputer device (via BLE host) and the
// LIVE preview page (buddy/references/ui_preview_live.html) read it
// back via GET /usage.
//
// Storage: INDEX KV, keyed by deviceHash. 1h TTL so stale hosts
// don't pin numbers on the device forever.

const KEY = (h) => `usage:${h}`;
const TTL_S = 3600;

// Native shape (what we store): per-provider {primary, secondary}.
// `primary`   is the short window (5h on Codex's ChatGPT-Plus plan).
// `secondary` is the long window  (7d).
// {
//   codex:  { primary: {used_percent, window_minutes, resets_at}, secondary: {...} },
//   claude: { primary: {...}, secondary: {...} },
//   ts: <server unix seconds>
// }
//
// Display shape (what GET returns): same providers, but each window
// pre-rendered for the LIVE page:
// {
//   codex:  { h5: {pct: 33, reset: "IN 2 HR 47 MIN"}, d7: {pct: 11, reset: "MON 12:00 AM"} },
//   claude: { h5: {...}, d7: {...} },
//   ts: ...
// }

function _bucket(raw) {
  if (!raw || typeof raw !== "object") return null;
  const out = {};
  for (const k of [
    "used_percent",
    "window_minutes",
    "resets_at",
    "resets_in_seconds",
  ]) {
    if (typeof raw[k] === "number") out[k] = raw[k];
  }
  return Object.keys(out).length ? out : null;
}

function _provider(raw) {
  if (!raw || typeof raw !== "object") return null;
  const primary = _bucket(raw.primary);
  const secondary = _bucket(raw.secondary);
  if (!primary && !secondary) return null;
  const out = {};
  if (primary) out.primary = primary;
  if (secondary) out.secondary = secondary;
  return out;
}

// ---- display rendering ---------------------------------------------

const DAY_NAMES = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"];

function _pct(b) {
  if (!b || typeof b.used_percent !== "number") return 0;
  return Math.max(0, Math.min(100, Math.round(b.used_percent)));
}

function _resetSeconds(b, nowS) {
  if (!b) return null;
  if (typeof b.resets_in_seconds === "number") return Math.max(0, b.resets_in_seconds);
  if (typeof b.resets_at === "number") return Math.max(0, b.resets_at - nowS);
  return null;
}

function _shortReset(b, nowS) {
  // "IN 4 HR 1 MIN" or "IN 12 MIN".
  const s = _resetSeconds(b, nowS);
  if (s == null) return "";
  if (s <= 0) return "NOW";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h > 0) return `IN ${h} HR ${m} MIN`;
  return `IN ${m} MIN`;
}

function _longReset(b, nowS) {
  // "MON 12:00 AM" — weekday + 12h clock in UTC. The device doesn't
  // know the user's tz, and UTC is unambiguous enough for a glance.
  const s = _resetSeconds(b, nowS);
  if (s == null) return "";
  if (s <= 0) return "NOW";
  const d = new Date((nowS + s) * 1000);
  const day = DAY_NAMES[d.getUTCDay()];
  let h = d.getUTCHours();
  const m = d.getUTCMinutes();
  const am = h < 12 ? "AM" : "PM";
  h = h % 12;
  if (h === 0) h = 12;
  return `${day} ${h}:${String(m).padStart(2, "0")} ${am}`;
}

function _render(provider, nowS) {
  if (!provider) return null;
  const p = provider.primary;
  const s = provider.secondary;
  return {
    h5: { pct: _pct(p), reset: _shortReset(p, nowS) },
    d7: { pct: _pct(s), reset: _longReset(s, nowS) },
  };
}

// ---- handlers -------------------------------------------------------

async function _load(env, hash) {
  if (!env.INDEX) return null;
  const raw = await env.INDEX.get(KEY(hash));
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export async function handleGetUsage(_request, env, auth) {
  const stored = (await _load(env, auth.deviceHash)) || {};
  const nowS = Math.floor(Date.now() / 1000);
  const out = { ts: stored.ts || null };
  for (const k of ["codex", "claude"]) {
    const rendered = _render(stored[k], nowS);
    if (rendered) out[k] = rendered;
  }
  return _json(out);
}

export async function handlePostUsage(request, env, auth) {
  const body = await request.json().catch(() => null);
  if (!body || typeof body !== "object") {
    return _json({ error: "bad_request", message: "expected JSON object" }, 400);
  }
  // Accept either:
  //   { codex: {primary, secondary}, claude: {...} }       (multi)
  //   { provider: "codex", primary: {...}, secondary: {...} } (single, legacy)
  //   { primary: {...}, secondary: {...} }                    (single, defaults to codex)
  const incoming = {};
  if (body.codex || body.claude) {
    if (body.codex) {
      const p = _provider(body.codex);
      if (p) incoming.codex = p;
    }
    if (body.claude) {
      const p = _provider(body.claude);
      if (p) incoming.claude = p;
    }
  } else {
    const which = body.provider === "claude" ? "claude" : "codex";
    const p = _provider(body);
    if (p) incoming[which] = p;
  }
  if (!Object.keys(incoming).length) {
    return _json(
      { error: "bad_request", message: "no primary/secondary buckets" },
      400,
    );
  }
  // Merge with existing so a codex-only POST doesn't clear claude.
  const stored = (await _load(env, auth.deviceHash)) || {};
  const next = { ...stored, ...incoming, ts: Math.floor(Date.now() / 1000) };
  if (env.INDEX) {
    await env.INDEX.put(KEY(auth.deviceHash), JSON.stringify(next), {
      expirationTtl: TTL_S,
    });
  }
  const nowS = next.ts;
  const display = { ts: nowS };
  for (const k of ["codex", "claude"]) {
    const rendered = _render(next[k], nowS);
    if (rendered) display[k] = rendered;
  }
  return _json({ ok: true, ...display });
}

// The LIVE preview HTML is typically opened via file:// so its
// fetches are cross-origin. We allow * because the endpoint is
// already gated by DEVICE_SECRET in a custom header — there's no
// cookie-based auth to protect against CSRF.
const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET, POST, OPTIONS",
  "access-control-allow-headers": "content-type, x-device-secret",
  "access-control-max-age": "86400",
};

export function handleUsagePreflight() {
  return new Response(null, { status: 204, headers: CORS });
}

function _json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json", ...CORS },
  });
}
