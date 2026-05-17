// Pager-facing HTTP handlers. Thin layer over the SessionRouter DO
// and the device session index. Auth is `x-device-secret` in headers
// (Cardputer) or `?token=...` in query string (browser fetches).
//
// Long-poll model: /pager/poll waits up to ~25s for new events. When
// the client is on the Detail screen we long-poll continuously; when
// on Inbox we short-poll the cheaper /pager/sessions list.

import { createSession as createAnthropicSession } from "./anthropic.js";
import { ensureAgentAndEnv } from "./setup.js";
import {
  forgetSession,
  getSessionMeta,
  hashDevice,
  listDeviceSessions,
  recordSession,
  updateSessionMeta,
} from "./index_kv.js";

const LONG_POLL_BUDGET_MS = 25_000;
const LONG_POLL_TICK_MS = 1_500;

function _routerStub(env, sessionId) {
  // One DO per session — name = sessionId. The Worker authorizes the
  // caller before forwarding, so the DO trusts everything it sees.
  const id = env.SESSION_ROUTER.idFromName(sessionId);
  return env.SESSION_ROUTER.get(id);
}

async function _doFetch(env, sessionId, action, init = {}) {
  const stub = _routerStub(env, sessionId);
  const url = `https://router/${action}` + (init._qs ? `?${init._qs}` : "");
  const req = new Request(url, {
    method: init.method || "GET",
    headers: { "content-type": "application/json" },
    body: init.body ? JSON.stringify(init.body) : undefined,
  });
  return stub.fetch(req);
}

async function _doJson(env, sessionId, action, init) {
  const resp = await _doFetch(env, sessionId, action, init);
  const text = await resp.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = { _raw: text };
  }
  return { status: resp.status, body };
}

// ---- Auth -----------------------------------------------------------

export async function authenticate(request, env) {
  const headerSecret = request.headers.get("x-device-secret");
  const url = new URL(request.url);
  const tokenSecret = url.searchParams.get("token");
  const provided = headerSecret || tokenSecret;
  if (!provided || !env.DEVICE_SECRET) return null;
  if (!_constantTimeEqual(provided, env.DEVICE_SECRET)) return null;
  const deviceHash = await hashDevice(provided);
  return { deviceSecret: provided, deviceHash };
}

function _constantTimeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

async function _ownsSession(env, deviceHash, sessionId) {
  const meta = await getSessionMeta(env, sessionId);
  return Boolean(meta && meta.deviceHash === deviceHash);
}

// ---- Routes ---------------------------------------------------------

export async function handleSpawn(request, env, auth) {
  const body = await request.json().catch(() => null);
  if (!body || typeof body.prompt !== "string" || !body.prompt.trim()) {
    return _json({ error: "bad_request", message: "missing prompt" }, 400);
  }

  const prompt = body.prompt.trim();
  const title = (body.title || prompt).split("\n")[0].slice(0, 72);
  const kind = body.kind || "task";
  const reuseSessionId = body.session_id || null;

  // Per-device daily spend guard: if the index records >= MAX_DAILY
  // sessions or recent usage exceeds DAILY_TOKEN_CAP, refuse new
  // spawns. This catches the "agent loop spawned a fork-bomb" path
  // before it can drain credits.
  // For v1 we keep it simple — just count spawns per UTC day.
  const today = new Date().toISOString().slice(0, 10);
  const spendKey = `spend:${auth.deviceHash}:${today}`;
  const spendRaw = await env.INDEX.get(spendKey);
  const spendCount = spendRaw ? parseInt(spendRaw, 10) : 0;
  const dailyMax = parseInt(env.PAGER_DAILY_SPAWN_CAP || "30", 10);
  if (!reuseSessionId && spendCount >= dailyMax) {
    return _json(
      {
        error: "rate_limited",
        message: `daily spawn cap (${dailyMax}) reached`,
        spend_count: spendCount,
      },
      429,
    );
  }

  // Resolve agent + env once per device. Cached after first call.
  const { agentId, environmentId } = await ensureAgentAndEnv(
    env,
    auth.deviceHash,
  );

  // If a session_id is supplied we're continuing an existing session
  // (resume / reply pattern from the Pager Detail screen).
  let sessionId = reuseSessionId;
  if (sessionId && !(await _ownsSession(env, auth.deviceHash, sessionId))) {
    return _json({ error: "forbidden" }, 403);
  }

  if (!sessionId) {
    // Fresh session: the Worker creates the Anthropic session
    // up-front so we know the real sessionId before addressing the
    // DO. The DO is named by sessionId, so doing it any other way
    // would force a copy from a synthetic-id DO into a canonical
    // one — racy and wasteful.
    const session = await createAnthropicSession(env.ANTHROPIC_API_KEY, {
      agent: agentId,
      environment_id: environmentId,
      title,
    });
    sessionId = session.id;

    // Hand the brand-new sessionId to the canonical DO along with
    // the first user message. The DO records meta and forwards the
    // message in one call.
    const resp = await _doFetch(env, sessionId, "spawn", {
      method: "POST",
      body: {
        sessionId,
        agentId,
        environmentId,
        deviceHash: auth.deviceHash,
        title,
        kind,
        prompt,
      },
    });
    if (!resp.ok) {
      return new Response(await resp.text(), { status: resp.status });
    }
  } else {
    // Resume — send a follow-up message to the existing session.
    const resp = await _doFetch(env, sessionId, "send", {
      method: "POST",
      body: { prompt },
    });
    if (!resp.ok) {
      return new Response(await resp.text(), { status: resp.status });
    }
  }

  await recordSession(env, auth.deviceHash, sessionId, {
    sessionId,
    deviceHash: auth.deviceHash,
    title,
    kind,
    createdAt: new Date().toISOString(),
  });

  if (!reuseSessionId) {
    await env.INDEX.put(spendKey, String(spendCount + 1), {
      expirationTtl: 86_400 * 7, // keep for a week so the UI can show recent days
    });
  }

  return _json({ ok: true, session_id: sessionId, title });
}

export async function handleSessions(request, env, auth) {
  const ids = await listDeviceSessions(env, auth.deviceHash);
  if (ids.length === 0) return _json({ sessions: [] });

  // Fetch summaries in parallel from each DO. Each call is cheap
  // (single DO storage read with optional upstream poll).
  const summaries = await Promise.all(
    ids.map(async (sid) => {
      const meta = await getSessionMeta(env, sid);
      if (!meta) return null;
      const { body, status } = await _doJson(env, sid, "summary");
      if (status !== 200 || !body?.summary) {
        return { session_id: sid, ...meta, summary: null };
      }
      return { session_id: sid, ...meta, summary: body.summary };
    }),
  );

  return _json({ sessions: summaries.filter(Boolean) });
}

// Long-poll: the client passes ?since=<seq> and we wait up to
// LONG_POLL_BUDGET_MS for the DO's seq to advance, returning either
// new events or an empty array on timeout. The DO ingests upstream
// inside _events on every call, so each tick gives Anthropic a chance
// to advance.
export async function handlePoll(request, env, auth) {
  const url = new URL(request.url);
  const sessionId = url.searchParams.get("session");
  if (!sessionId) return _json({ error: "missing session" }, 400);
  if (!(await _ownsSession(env, auth.deviceHash, sessionId))) {
    return _json({ error: "forbidden" }, 403);
  }
  const since = parseInt(url.searchParams.get("since") || "0", 10);
  const compact = url.searchParams.get("compact") === "1";
  // The Cardputer is a 240×135 device with limited socket RAM; large
  // payloads (e.g. a multi-KB agent.message) cause MicroPython's
  // requests to ECONNABORT mid-read. Cap aggressively in compact mode.
  const defaultLimit = compact ? "30" : "200";
  const limit = Math.min(
    parseInt(url.searchParams.get("limit") || defaultLimit, 10),
    500,
  );
  const wait = url.searchParams.get("wait") !== "0";
  const budgetMs = wait
    ? Math.min(
        parseInt(
          url.searchParams.get("budget_ms") || `${LONG_POLL_BUDGET_MS}`,
          10,
        ),
        28_000,
      )
    : 0;

  const start = Date.now();
  let payload = await _doJson(env, sessionId, "events", {
    _qs: `since=${since}&limit=${limit}`,
  });
  if (payload.status !== 200)
    return new Response(JSON.stringify(payload.body), {
      status: payload.status,
    });

  while (
    wait &&
    payload.body?.events?.length === 0 &&
    Date.now() - start < budgetMs &&
    payload.body?.summary?.status !== "terminated"
  ) {
    await _sleep(LONG_POLL_TICK_MS);
    payload = await _doJson(env, sessionId, "events", {
      _qs: `since=${since}&limit=${limit}`,
    });
    if (payload.status !== 200)
      return new Response(JSON.stringify(payload.body), {
        status: payload.status,
      });
  }

  if (compact && payload.body) {
    payload.body = _toCompactPayload(payload.body);
  }
  return _json(payload.body);
}

// Strip event payloads down to what the Cardputer's tiny ticker UI
// actually renders. Drops large fields (full message text, tool
// outputs, thinking traces) that the device truncates anyway. Keeps
// the on-wire payload from blowing up MicroPython's SSL read buffer.
function _toCompactPayload(body) {
  const events = (body.events || []).map((ev) => {
    const t = ev.type;
    const p = ev.payload || {};
    let cp = {};
    if (t === "agent.message") {
      const text = (p.content || [])
        .filter((b) => b.type === "text")
        .map((b) => b.text || "")
        .join("")
        .slice(-200); // last 200 chars; agent's punchline is at the end
      cp = { content: [{ type: "text", text }] };
    } else if (
      t === "agent.tool_use" ||
      t === "agent.mcp_tool_use" ||
      t === "agent.custom_tool_use"
    ) {
      const inp = p.input || {};
      const compactInput = {};
      if (typeof inp.command === "string") {
        compactInput.command = inp.command.split("\n")[0].slice(0, 120);
      }
      if (typeof inp.path === "string")
        compactInput.path = inp.path.slice(0, 120);
      cp = { name: p.name, input: compactInput };
    } else if (t === "session.status_idle") {
      cp = { stop_reason: p.stop_reason || null };
    } else if (t === "session.error") {
      cp = { error: { message: (p.error || {}).message || "(unspecified)" } };
    } else if (t === "agent.thinking") {
      cp = {}; // device renders a single "…" sigil; no body needed
    }
    return { seq: ev.seq, id: ev.id, type: t, ts: ev.ts, payload: cp };
  });
  return { ...body, events };
}

export async function handleInterrupt(request, env, auth) {
  const body = await request.json().catch(() => ({}));
  const sessionId = body.session_id;
  if (!sessionId) return _json({ error: "missing session_id" }, 400);
  if (!(await _ownsSession(env, auth.deviceHash, sessionId))) {
    return _json({ error: "forbidden" }, 403);
  }
  const resp = await _doFetch(env, sessionId, "interrupt", {
    method: "POST",
    body: { prompt: body.prompt || null },
  });
  return new Response(await resp.text(), {
    status: resp.status,
    headers: { "content-type": "application/json" },
  });
}

export async function handleReply(request, env, auth) {
  const body = await request.json().catch(() => ({}));
  const sessionId = body.session_id;
  const prompt = String(body.prompt || "").trim();
  if (!sessionId || !prompt)
    return _json({ error: "missing session_id or prompt" }, 400);
  if (!(await _ownsSession(env, auth.deviceHash, sessionId))) {
    return _json({ error: "forbidden" }, 403);
  }
  const resp = await _doFetch(env, sessionId, "send", {
    method: "POST",
    body: { prompt },
  });
  return new Response(await resp.text(), {
    status: resp.status,
    headers: { "content-type": "application/json" },
  });
}

export async function handleConfirm(request, env, auth) {
  const body = await request.json().catch(() => ({}));
  const sessionId = body.session_id;
  const toolUseId = body.tool_use_id;
  const approve = Boolean(body.approve);
  if (!sessionId || !toolUseId) return _json({ error: "missing fields" }, 400);
  if (!(await _ownsSession(env, auth.deviceHash, sessionId))) {
    return _json({ error: "forbidden" }, 403);
  }
  const resp = await _doFetch(env, sessionId, "send", {
    method: "POST",
    body: {
      events: [
        {
          type: "user.tool_confirmation",
          tool_use_id: toolUseId,
          decision: approve ? "approve" : "deny",
        },
      ],
    },
  });
  return new Response(await resp.text(), {
    status: resp.status,
    headers: { "content-type": "application/json" },
  });
}

export async function handleDelete(request, env, auth) {
  const body = await request.json().catch(() => ({}));
  const sessionId = body.session_id;
  if (!sessionId) return _json({ error: "missing session_id" }, 400);
  if (!(await _ownsSession(env, auth.deviceHash, sessionId))) {
    return _json({ error: "forbidden" }, 403);
  }
  const resp = await _doFetch(env, sessionId, "delete", { method: "POST" });
  await forgetSession(env, auth.deviceHash, sessionId);
  return new Response(await resp.text(), {
    status: resp.status,
    headers: { "content-type": "application/json" },
  });
}

export async function handleRename(request, env, auth) {
  const body = await request.json().catch(() => ({}));
  const sessionId = body.session_id;
  const title = String(body.title || "")
    .trim()
    .slice(0, 72);
  if (!sessionId || !title) return _json({ error: "missing fields" }, 400);
  if (!(await _ownsSession(env, auth.deviceHash, sessionId))) {
    return _json({ error: "forbidden" }, 403);
  }
  const next = await updateSessionMeta(env, sessionId, { title });
  return _json({ ok: true, meta: next });
}

// ---- Helpers --------------------------------------------------------

function _json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function _sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}
