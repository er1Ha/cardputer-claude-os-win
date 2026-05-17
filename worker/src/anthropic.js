// Thin Anthropic Managed Agents API client used by the Worker and the
// SessionRouter Durable Object. Centralises the beta header, base URL,
// auth, and a few normalisations that downstream code expects.
//
// All endpoints documented at:
//   https://platform.claude.com/docs/en/managed-agents/overview
//
// We only depend on global fetch — no SDK — so the same code runs in
// the Worker isolate and in the DO isolate without bundler tricks.

const BASE = "https://api.anthropic.com";
const VERSION = "2023-06-01";
const BETA = "managed-agents-2026-04-01";

function headers(apiKey) {
  return {
    "x-api-key": apiKey,
    "anthropic-version": VERSION,
    "anthropic-beta": BETA,
    "content-type": "application/json",
  };
}

async function _json(resp) {
  const text = await resp.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return { _raw: text };
  }
}

class AnthropicError extends Error {
  constructor(status, body) {
    super(
      `anthropic ${status}: ${typeof body === "string" ? body : JSON.stringify(body).slice(0, 300)}`,
    );
    this.status = status;
    this.body = body;
  }
}

async function _call(apiKey, method, path, body) {
  const init = { method, headers: headers(apiKey) };
  if (body !== undefined) init.body = JSON.stringify(body);
  const resp = await fetch(BASE + path, init);
  const parsed = await _json(resp);
  if (!resp.ok) throw new AnthropicError(resp.status, parsed ?? "(empty)");
  return parsed;
}

// ---- Agents ---------------------------------------------------------

export async function createAgent(apiKey, body) {
  return _call(apiKey, "POST", "/v1/agents", body);
}

export async function getAgent(apiKey, agentId) {
  return _call(apiKey, "GET", `/v1/agents/${agentId}`);
}

// ---- Environments ---------------------------------------------------

export async function createEnvironment(apiKey, body) {
  return _call(apiKey, "POST", "/v1/environments", body);
}

// ---- Sessions -------------------------------------------------------

export async function createSession(apiKey, body) {
  return _call(apiKey, "POST", "/v1/sessions", body);
}

export async function getSession(apiKey, sessionId) {
  return _call(apiKey, "GET", `/v1/sessions/${sessionId}`);
}

export async function deleteSession(apiKey, sessionId) {
  // 204 — no body expected.
  const resp = await fetch(`${BASE}/v1/sessions/${sessionId}`, {
    method: "DELETE",
    headers: headers(apiKey),
  });
  if (!resp.ok && resp.status !== 404) {
    throw new AnthropicError(resp.status, await resp.text());
  }
  return { ok: true };
}

export async function archiveSession(apiKey, sessionId) {
  return _call(apiKey, "POST", `/v1/sessions/${sessionId}/archive`);
}

// ---- Events ---------------------------------------------------------

export async function sendEvents(apiKey, sessionId, events) {
  return _call(apiKey, "POST", `/v1/sessions/${sessionId}/events`, { events });
}

// List event history for a session. Anthropic's events list uses
// `created_at[gt]=<ISO ts>` as the watermark cursor and an opaque
// `page` token for cross-page continuation. We fetch oldest-first
// (`order=asc`) so the natural array order is also the apply order.
//
// We do a single page per call (limit defaults to 100) and rely on
// the next poll tick's watermark to drain any tail. With sub-second
// poll cadence and rare bursts above 100 events, this is simpler
// than threading the opaque page cursor through DO storage.
//
// `sinceTs` is the ISO timestamp of the most recently ingested event.
// Pass `null` to fetch from the start of the session.
export async function listEvents(
  apiKey,
  sessionId,
  { sinceTs = null, limit = 100 } = {},
) {
  const qs = new URLSearchParams();
  qs.set("limit", String(limit));
  qs.set("order", "asc");
  if (sinceTs) qs.set("created_at[gt]", sinceTs);
  const data = await _call(
    apiKey,
    "GET",
    `/v1/sessions/${sessionId}/events?${qs}`,
  );
  return Array.isArray(data?.data) ? data.data : [];
}

// ---- Files ----------------------------------------------------------

export async function listFiles(apiKey, scopeId) {
  const qs = new URLSearchParams({ scope_id: scopeId });
  return _call(apiKey, "GET", `/v1/files?${qs}`);
}

// Returns a Response so callers can stream the body directly to the
// HTTP client without buffering the entire file into Worker RAM.
export async function downloadFile(apiKey, fileId) {
  const resp = await fetch(`${BASE}/v1/files/${fileId}/content`, {
    headers: {
      "x-api-key": apiKey,
      "anthropic-version": VERSION,
      "anthropic-beta": BETA,
    },
  });
  if (!resp.ok) {
    throw new AnthropicError(resp.status, await resp.text());
  }
  return resp;
}

export { AnthropicError };
