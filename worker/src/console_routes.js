// Console-facing HTTP handlers. Serves the Central Console HTML and
// the SSE bridge that powers it.
//
// Auth: same DEVICE_SECRET as the Pager. The HTML page asks for the
// secret on first load and stores it in localStorage; subsequent
// fetches pass it via `?token=...`. This keeps the secret out of any
// shared logs while still being a single value to manage.
//
// SSE bridge: rather than proxying Anthropic's stream directly (which
// requires us to hold an upstream SSE per session), we drive an SSE
// loop server-side: poll the DO every ~1s, push any new events to
// the client. The DO's _ingestUpstream coalesces underneath, so this
// scales fine across many open tabs.

import { downloadFile, listFiles } from "./anthropic.js";
import { getSessionMeta } from "./index_kv.js";
import CONSOLE_HTML from "./console.html";

const SSE_TICK_MS = 1_000;
// We send a heartbeat comment every HEARTBEAT_MS so intermediaries
// (and the browser's EventSource) don't tear the connection down on
// idle sessions. EventSource auto-reconnects but we want to avoid
// the "session disconnected" flash in the UI.
const HEARTBEAT_MS = 15_000;
// Max wall-clock for one SSE response. After this we close cleanly
// and the browser reconnects (carrying its `since` cursor). Keeps
// us comfortably under any intermediate proxy timeouts.
const MAX_STREAM_MS = 4 * 60_000;

function _routerStub(env, sessionId) {
  const id = env.SESSION_ROUTER.idFromName(sessionId);
  return env.SESSION_ROUTER.get(id);
}

async function _doJson(env, sessionId, action, qs) {
  const url = `https://router/${action}` + (qs ? `?${qs}` : "");
  const stub = _routerStub(env, sessionId);
  const resp = await stub.fetch(new Request(url));
  const text = await resp.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = { _raw: text };
  }
  return { status: resp.status, body };
}

// ---- Page -----------------------------------------------------------

export function handleConsolePage() {
  return new Response(CONSOLE_HTML, {
    status: 200,
    headers: {
      "content-type": "text/html; charset=utf-8",
      // Tight CSP — the page is self-contained and only talks to its
      // own origin. No external CDNs, no inline event handlers.
      "content-security-policy":
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:;",
      "x-content-type-options": "nosniff",
      "referrer-policy": "no-referrer",
    },
  });
}

// ---- SSE bridge -----------------------------------------------------

export async function handleStream(request, env, auth) {
  const url = new URL(request.url);
  const sessionId = url.searchParams.get("session");
  if (!sessionId) return _json({ error: "missing session" }, 400);

  const meta = await getSessionMeta(env, sessionId);
  if (!meta || meta.deviceHash !== auth.deviceHash) {
    return _json({ error: "forbidden" }, 403);
  }

  let since = parseInt(url.searchParams.get("since") || "0", 10);
  const startedAt = Date.now();

  // Build an SSE response with a streamed body. We loop server-side,
  // poll the DO once per tick, and write deltas to the client. Closes
  // cleanly at MAX_STREAM_MS — the browser will reconnect and resume.
  const { readable, writable } = new TransformStream();
  const writer = writable.getWriter();
  const enc = new TextEncoder();
  const send = async (event, data) => {
    const payload = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
    await writer.write(enc.encode(payload));
  };

  // Detached pump — the response is returned immediately so the
  // client starts receiving headers, then the body fills as we go.
  (async () => {
    let lastHeartbeat = Date.now();
    try {
      // Initial snapshot so a freshly-attached browser tab paints
      // immediately, even if there are zero new events.
      const initial = await _doJson(
        env,
        sessionId,
        "events",
        `since=${since}&limit=500`,
      );
      if (initial.status === 200) {
        await send("snapshot", {
          meta: initial.body.meta,
          summary: initial.body.summary,
          events: initial.body.events,
          seq: initial.body.seq,
        });
        since = initial.body.seq || since;
      } else {
        await send("error", initial.body);
        await writer.close();
        return;
      }

      while (Date.now() - startedAt < MAX_STREAM_MS) {
        await _sleep(SSE_TICK_MS);
        const next = await _doJson(
          env,
          sessionId,
          "events",
          `since=${since}&limit=200`,
        );
        if (next.status !== 200) {
          await send("error", next.body);
          break;
        }
        const events = next.body?.events || [];
        if (events.length > 0) {
          await send("events", {
            summary: next.body.summary,
            events,
            seq: next.body.seq,
          });
          since = next.body.seq || since;
          lastHeartbeat = Date.now();
        } else if (Date.now() - lastHeartbeat > HEARTBEAT_MS) {
          // SSE comment lines (lines beginning with ":") are ignored
          // by the EventSource parser but keep the connection alive.
          await writer.write(enc.encode(": hb\n\n"));
          lastHeartbeat = Date.now();
          // Also send a summary refresh so the UI's status pill
          // animates even when the agent is silent.
          await send("summary", {
            summary: next.body.summary,
            seq: next.body.seq,
          });
        }
        // Stop early if the session is terminated — no point polling
        // a dead session. The browser will reconnect and rediscover
        // status from the snapshot.
        if (next.body?.summary?.status === "terminated") {
          await send("done", { reason: "terminated" });
          break;
        }
      }
    } catch (err) {
      try {
        await send("error", { message: String(err?.message || err) });
      } catch {
        /* writer may be closed */
      }
    } finally {
      try {
        await writer.close();
      } catch {
        /* already closed */
      }
    }
  })();

  return new Response(readable, {
    status: 200,
    headers: {
      "content-type": "text/event-stream",
      "cache-control": "no-cache, no-transform",
      "x-accel-buffering": "no",
      connection: "keep-alive",
    },
  });
}

// ---- Files API proxy -----------------------------------------------

export async function handleFilesList(request, env, auth) {
  const url = new URL(request.url);
  const sessionId = url.searchParams.get("session");
  if (!sessionId) return _json({ error: "missing session" }, 400);
  const meta = await getSessionMeta(env, sessionId);
  if (!meta || meta.deviceHash !== auth.deviceHash) {
    return _json({ error: "forbidden" }, 403);
  }

  const data = await listFiles(env.ANTHROPIC_API_KEY, sessionId).catch(
    (err) => ({
      _err: { status: err.status || 502, message: err.message },
    }),
  );
  if (data?._err) return _json(data._err, data._err.status);

  const files = (data?.data || []).map((f) => ({
    id: f.id,
    filename: f.filename,
    size: f.size_bytes ?? f.size ?? null,
    ts: f.created_at || null,
  }));
  return _json({ files });
}

export async function handleFileDownload(request, env, auth) {
  const url = new URL(request.url);
  const sessionId = url.searchParams.get("session");
  const fileId = url.searchParams.get("file_id");
  if (!sessionId || !fileId) return _json({ error: "missing fields" }, 400);
  const meta = await getSessionMeta(env, sessionId);
  if (!meta || meta.deviceHash !== auth.deviceHash) {
    return _json({ error: "forbidden" }, 403);
  }

  const upstream = await downloadFile(env.ANTHROPIC_API_KEY, fileId);
  // Pass body through directly — no Worker RAM buffering. Forward
  // the relevant content headers so the browser / claude-pull can
  // size and name the file correctly.
  const passThrough = new Headers();
  for (const h of [
    "content-type",
    "content-length",
    "content-disposition",
    "etag",
    "last-modified",
  ]) {
    const v = upstream.headers.get(h);
    if (v) passThrough.set(h, v);
  }
  // Default to a sane content-type if upstream didn't set one.
  if (!passThrough.has("content-type")) {
    passThrough.set("content-type", "application/octet-stream");
  }
  return new Response(upstream.body, {
    status: upstream.status,
    headers: passThrough,
  });
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
