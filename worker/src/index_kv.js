// Device-level session index, stored in the INDEX KV namespace.
//
// Per-session state lives inside the SessionRouter Durable Object,
// but we still need a flat list of "what sessions exist for this
// device" so the Pager Inbox and the Console session list can render
// without enumerating all DOs. KV is a fine fit: rare writes
// (session create / delete / title rename), frequent reads.

const SESSIONS_KEY = (h) => `idx:${h}:sessions`;
const META_KEY = (sid) => `sess:${sid}:meta`;
const REVERSE_KEY = (sid) => `sess:${sid}:device`;

const MAX_SESSIONS_PER_DEVICE = 50;

export async function listDeviceSessions(env, deviceHash) {
  const raw = await env.INDEX.get(SESSIONS_KEY(deviceHash));
  if (!raw) return [];
  try {
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

export async function getSessionMeta(env, sessionId) {
  const raw = await env.INDEX.get(META_KEY(sessionId));
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export async function recordSession(env, deviceHash, sessionId, meta) {
  const existing = await listDeviceSessions(env, deviceHash);
  // De-dup and prepend.
  const next = [sessionId, ...existing.filter((s) => s !== sessionId)].slice(
    0,
    MAX_SESSIONS_PER_DEVICE,
  );
  await Promise.all([
    env.INDEX.put(SESSIONS_KEY(deviceHash), JSON.stringify(next)),
    env.INDEX.put(META_KEY(sessionId), JSON.stringify(meta)),
    env.INDEX.put(REVERSE_KEY(sessionId), deviceHash),
  ]);
}

export async function updateSessionMeta(env, sessionId, patch) {
  const existing = (await getSessionMeta(env, sessionId)) || {};
  const next = { ...existing, ...patch };
  await env.INDEX.put(META_KEY(sessionId), JSON.stringify(next));
  return next;
}

export async function forgetSession(env, deviceHash, sessionId) {
  const existing = await listDeviceSessions(env, deviceHash);
  const next = existing.filter((s) => s !== sessionId);
  await Promise.all([
    env.INDEX.put(SESSIONS_KEY(deviceHash), JSON.stringify(next)),
    env.INDEX.delete(META_KEY(sessionId)),
    env.INDEX.delete(REVERSE_KEY(sessionId)),
  ]);
}

export async function deviceForSession(env, sessionId) {
  return env.INDEX.get(REVERSE_KEY(sessionId));
}

// Stable, deterministic, non-reversible device key. Bound to the
// device secret so two devices with different secrets get separate
// session lists, but we never write the raw secret into KV. Same
// hash is used as the DO name suffix for ownership checks.
export async function hashDevice(secret) {
  const data = new TextEncoder().encode("cardputer-pager:v1:" + secret);
  const buf = await crypto.subtle.digest("SHA-256", data);
  const bytes = new Uint8Array(buf);
  let hex = "";
  for (let i = 0; i < bytes.length; i++) {
    hex += bytes[i].toString(16).padStart(2, "0");
  }
  return hex.slice(0, 32);
}
