// Codex usage cache.
//
// The host-side `codex-usage` script scans `~/.codex/sessions/` for the
// latest `token_count` event, extracts its `rate_limits` block, and
// POSTs the result here. The device (or any UI) reads it back via GET.
//
// Storage: INDEX KV, keyed by deviceHash. TTL 1h so a host that goes
// offline doesn't leave stale numbers on the device forever.

const USAGE_KEY = (h) => `usage:${h}`;
const USAGE_TTL_S = 3600;

// Shape we accept on POST and return on GET. Mirrors Codex CLI's
// own `rate_limits` block; windows are stored in minutes so the
// consumer can label them (300 → 5h, 10080 → 7d) without us baking
// the labels in here.
//
// {
//   primary:   { used_percent: 33.0, window_minutes: 300,   resets_at: 1779106167 },
//   secondary: { used_percent: 11.0, window_minutes: 10080, resets_at: 1779643085 },
//   ts: <unix seconds, server-set>
// }
//
// `resets_at` is Codex CLI's native shape (unix seconds). Older shapes
// using `resets_in_seconds` are passed through too for robustness.

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

export async function handleGetUsage(_request, env, auth) {
  if (!env.INDEX) return _json({});
  const raw = await env.INDEX.get(USAGE_KEY(auth.deviceHash));
  if (!raw) return _json({});
  try {
    return _json(JSON.parse(raw));
  } catch {
    return _json({});
  }
}

export async function handlePostUsage(request, env, auth) {
  const body = await request.json().catch(() => null);
  if (!body || typeof body !== "object") {
    return _json({ error: "bad_request", message: "expected JSON object" }, 400);
  }
  const payload = {};
  const primary = _bucket(body.primary);
  const secondary = _bucket(body.secondary);
  if (primary) payload.primary = primary;
  if (secondary) payload.secondary = secondary;
  if (!primary && !secondary) {
    return _json(
      { error: "bad_request", message: "no primary/secondary buckets" },
      400,
    );
  }
  payload.ts = Math.floor(Date.now() / 1000);
  if (env.INDEX) {
    await env.INDEX.put(USAGE_KEY(auth.deviceHash), JSON.stringify(payload), {
      expirationTtl: USAGE_TTL_S,
    });
  }
  return _json({ ok: true, ...payload });
}

function _json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json" },
  });
}
