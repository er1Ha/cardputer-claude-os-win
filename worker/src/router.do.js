// SessionRouter Durable Object — owns one Managed Agents session.
//
// Responsibility split:
//   - The Worker handles HTTP, auth, and routing.
//   - The DO owns: the upstream event cursor, the local event log,
//     the summary blob, the file manifest, and (on idle) the file
//     mirror trigger.
//
// We do NOT hold a long-lived SSE upstream. Instead the DO polls
// Anthropic's `events.list` endpoint with `after_id`, lazily on
// every read. This is dramatically simpler than orchestrating a
// persistent SSE upstream from a hibernating Worker isolate, and
// the API rate limit (600 reads/min/org) is comfortably above
// what a single-user device can produce.
//
// Storage layout (DO transactional storage):
//   meta       → {sessionId, agentId, environmentId, deviceHash, title, kind, createdAt}
//   summary    → see _emptySummary()
//   cursor     → last Anthropic event id we have ingested (or null)
//   seq        → monotonic local event counter (number)
//   evt:<seq>  → normalized event {seq, id, type, ts, payload}
//   files      → [{id, filename, size, ts}]
//   archived   → bool
//
// The local seq is what clients use as their "since" cursor when
// long-polling. It's monotonic and dense — easy to bound responses
// and dedupe missed events.

import {
  AnthropicError,
  archiveSession as anthropicArchiveSession,
  deleteSession,
  getSession,
  listEvents,
  listFiles,
  sendEvents,
} from "./anthropic.js";

const EVT_PREFIX = "evt:";
// Hard cap on how many events we keep per session in DO storage.
// 1000 events is plenty for the Pager + Console UX (the typical
// session is dozens to low hundreds), and well under DO storage
// per-key/per-DO limits.
const MAX_EVENTS = 1000;
// Min interval between upstream polls. Coalesces a flurry of client
// requests into one upstream call. 600 ms gives sub-second perceived
// freshness while staying well under Anthropic's rate limit even
// across many active sessions.
const POLL_COALESCE_MS = 600;
// Max we'll fetch in one ingest pass before yielding back to the
// client. Pages beyond this stay in Anthropic's history and get
// drained on the next call.
const MAX_INGEST_PER_TICK = 500;

function _emptySummary() {
  return {
    status: "idle", // idle | running | rescheduling | terminated
    statusReason: null, // last stop_reason from session.status_idle
    lastTool: null, // {name, input_summary} for the most recent tool_use
    lastText: "", // last assistant text snippet, ~120 chars
    lastEventTs: 0,
    pendingConfirm: null, // {tool_use_id, tool} when waiting on user.tool_confirmation
    fileCount: 0,
    seq: 0,
    usage: null, // {input_tokens, output_tokens, ...} from session
    error: null, // last session.error payload
  };
}

// Normalise an upstream event into a compact shape the Pager and
// Console can both render. We keep the original `id` so dedup works
// across re-ingests, and keep the full payload for the Console (it
// needs tool inputs/outputs to render diffs and bash blocks). The
// Pager only ever reads the projected fields out of `summary`.
function _normalize(e, seq) {
  return {
    seq,
    id: e.id,
    type: e.type,
    ts: e.created_at || e.processed_at || new Date().toISOString(),
    payload: e,
  };
}

// Project a single upstream event onto the running summary blob.
// Pure function except for mutating the passed-in summary; called
// once per ingested event in arrival order so order matters.
function _projectIntoSummary(summary, e) {
  summary.lastEventTs = Date.now();

  switch (e.type) {
    case "session.status_running":
      summary.status = "running";
      break;
    case "session.status_idle":
      summary.status = "idle";
      summary.statusReason = e.stop_reason || null;
      // A status_idle clears any pending confirmation that wasn't
      // taken — the agent moved on.
      summary.pendingConfirm = null;
      break;
    case "session.status_rescheduled":
      summary.status = "rescheduling";
      break;
    case "session.status_terminated":
      summary.status = "terminated";
      break;
    case "session.error":
      summary.error = {
        message: e.error?.message || "(unspecified)",
        retryStatus: e.error?.retry_status || null,
      };
      break;
    case "agent.message": {
      const blocks = Array.isArray(e.content) ? e.content : [];
      const text = blocks
        .filter((b) => b.type === "text")
        .map((b) => b.text)
        .join("")
        .trim();
      if (text) {
        // Keep the *last* line of the most recent message — most
        // useful for the pager ticker since the agent often ends
        // with the punchline.
        const last = text.split(/\n+/).filter(Boolean).pop() || text;
        summary.lastText = last.slice(0, 200);
      }
      break;
    }
    case "agent.tool_use":
    case "agent.mcp_tool_use":
    case "agent.custom_tool_use": {
      const name = e.name || "tool";
      const input = e.input || {};
      let summaryStr = "";
      // Pretty-print the most useful slice of common tools so the
      // pager line reads naturally ("bash: pytest tests/").
      if (name === "bash" && typeof input.command === "string") {
        summaryStr = input.command.split("\n")[0].slice(0, 80);
      } else if (
        (name === "str_replace" || name === "create" || name === "edit") &&
        typeof input.path === "string"
      ) {
        summaryStr = input.path;
      } else if (name === "view" && typeof input.path === "string") {
        summaryStr = input.path;
      } else {
        // Generic fallback: first scalar value of the input object.
        for (const [, v] of Object.entries(input)) {
          if (typeof v === "string") {
            summaryStr = v.slice(0, 80);
            break;
          }
        }
      }
      summary.lastTool = { name, summary: summaryStr };
      break;
    }
    case "user.tool_confirmation":
      // Consumed — the agent received our answer.
      summary.pendingConfirm = null;
      break;
    case "agent.tool_use_pending_confirmation":
    case "session.tool_confirmation_required": {
      // The Anthropic API exposes pending-confirmation under one of
      // a couple of event names depending on policy shape; handle
      // either by keying on the carried tool reference.
      const tu = e.tool_use || {};
      summary.pendingConfirm = {
        toolUseId: e.tool_use_id || tu.id || null,
        name: tu.name || e.name || "tool",
      };
      break;
    }
    default:
      // Other events (thinking, thread_*, tool_result, etc.) flow
      // through to the Console without touching the summary.
      break;
  }
}

export class SessionRouter {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.lastPollMs = 0;
    this.pollPromise = null; // for coalescing
  }

  async fetch(request) {
    const url = new URL(request.url);
    const action =
      url.searchParams.get("action") || url.pathname.replace(/^\//, "");
    try {
      switch (action) {
        case "spawn":
          return this._spawn(request);
        case "summary":
          return this._summary();
        case "events":
          return this._events(url);
        case "send":
          return this._send(request);
        case "interrupt":
          return this._interrupt(request);
        case "delete":
          return this._delete();
        case "files":
          return this._files();
        case "meta":
          return this._meta();
        default:
          return new Response("unknown action", { status: 404 });
      }
    } catch (err) {
      // Surface AnthropicError details cleanly so the Pager can show
      // them; everything else becomes a generic 500 with the message
      // (no stack — these run in production).
      const status = err instanceof AnthropicError ? err.status : 500;
      const body = {
        error: err instanceof AnthropicError ? "anthropic" : "internal",
        status,
        message: String(err?.message || err),
        detail: err?.body ?? null,
      };
      return new Response(JSON.stringify(body), {
        status: status >= 400 && status < 600 ? status : 500,
        headers: { "content-type": "application/json" },
      });
    }
  }

  // ---- Public actions ------------------------------------------------

  // Spawn / init / kick all go through the same path:
  //   - If meta isn't stored yet, write it (using the supplied
  //     sessionId, since the Worker addresses this DO by sessionId).
  //   - If a prompt is provided, send a user.message.
  // The Worker creates the Anthropic session up-front so it can
  // address this DO by the real sessionId. We never call
  // createSession from the DO itself — that would race the
  // Worker→DO routing.
  async _spawn(request) {
    const body = await request.json();
    const {
      sessionId,
      agentId,
      environmentId,
      deviceHash,
      title,
      kind,
      prompt,
    } = body;
    if (!sessionId) return _bad("missing sessionId");

    const existing = await this.state.storage.get("meta");
    let meta = existing || null;
    if (!meta) {
      meta = {
        sessionId,
        agentId,
        environmentId,
        deviceHash,
        title: (title || prompt || "").slice(0, 72) || sessionId,
        kind: kind || "task",
        createdAt: new Date().toISOString(),
      };
      await this.state.storage.put("meta", meta);
      await this.state.storage.put("summary", _emptySummary());
      await this.state.storage.put("seq", 0);
    } else if (meta.sessionId !== sessionId) {
      // Someone tried to reuse a DO instance for a different session.
      // The DO is named by sessionId so this should never happen, but
      // surface a clear error rather than silently corrupting state.
      return _bad("sessionId mismatch", 409);
    }

    if (prompt && prompt.length > 0) {
      await sendEvents(this.env.ANTHROPIC_API_KEY, meta.sessionId, [
        {
          type: "user.message",
          content: [{ type: "text", text: prompt }],
        },
      ]);
      // Optimistically mark running so the next Pager poll doesn't
      // show stale "idle" until the upstream tick lands.
      const summary =
        (await this.state.storage.get("summary")) || _emptySummary();
      summary.status = "running";
      summary.lastEventTs = Date.now();
      await this.state.storage.put("summary", summary);
    }

    return _json({ ok: true, sessionId: meta.sessionId });
  }

  async _summary() {
    await this._ingestUpstream();
    const meta = await this.state.storage.get("meta");
    const summary =
      (await this.state.storage.get("summary")) || _emptySummary();
    return _json({ meta, summary });
  }

  async _events(url) {
    const since = parseInt(url.searchParams.get("since") || "0", 10);
    const limit = Math.min(
      parseInt(url.searchParams.get("limit") || "200", 10),
      500,
    );
    await this._ingestUpstream();
    const out = await this._readEventsSince(since, limit);
    const summary =
      (await this.state.storage.get("summary")) || _emptySummary();
    const meta = await this.state.storage.get("meta");
    return _json({ meta, summary, events: out, seq: summary.seq });
  }

  async _send(request) {
    const body = await request.json();
    const meta = await this.state.storage.get("meta");
    if (!meta) return _bad("session not found", 404);
    const events = body.events || [
      {
        type: "user.message",
        content: [{ type: "text", text: String(body.prompt || "") }],
      },
    ];
    await sendEvents(this.env.ANTHROPIC_API_KEY, meta.sessionId, events);
    return _json({ ok: true });
  }

  async _interrupt(request) {
    const body = await request.json().catch(() => ({}));
    const meta = await this.state.storage.get("meta");
    if (!meta) return _bad("session not found", 404);
    const events = [{ type: "user.interrupt" }];
    if (body.prompt) {
      events.push({
        type: "user.message",
        content: [{ type: "text", text: String(body.prompt) }],
      });
    }
    await sendEvents(this.env.ANTHROPIC_API_KEY, meta.sessionId, events);
    return _json({ ok: true });
  }

  async _delete() {
    const meta = await this.state.storage.get("meta");
    if (meta?.sessionId) {
      try {
        await deleteSession(this.env.ANTHROPIC_API_KEY, meta.sessionId);
      } catch (err) {
        // If the session is currently `running`, Anthropic returns a
        // 409 and asks for an interrupt first. Try that, then retry.
        if (err instanceof AnthropicError && err.status === 409) {
          try {
            await sendEvents(this.env.ANTHROPIC_API_KEY, meta.sessionId, [
              { type: "user.interrupt" },
            ]);
          } catch {
            /* best-effort */
          }
          try {
            await anthropicArchiveSession(
              this.env.ANTHROPIC_API_KEY,
              meta.sessionId,
            );
          } catch {
            /* best-effort */
          }
        } else {
          throw err;
        }
      }
    }
    await this.state.storage.deleteAll();
    return _json({ ok: true });
  }

  async _files() {
    const meta = await this.state.storage.get("meta");
    if (!meta) return _bad("session not found", 404);
    // Refresh from upstream — the manifest can change mid-session
    // even before idle (e.g. agent writes a file early and the user
    // wants to grab it).
    const data = await listFiles(this.env.ANTHROPIC_API_KEY, meta.sessionId);
    const files = (data?.data || []).map((f) => ({
      id: f.id,
      filename: f.filename,
      size: f.size_bytes ?? f.size ?? null,
      ts: f.created_at || null,
    }));
    await this.state.storage.put("files", files);
    return _json({ files });
  }

  async _meta() {
    const meta = await this.state.storage.get("meta");
    if (!meta) return _bad("session not found", 404);
    const summary =
      (await this.state.storage.get("summary")) || _emptySummary();
    return _json({ meta, summary });
  }

  // ---- Internals -----------------------------------------------------

  // Pull new events from Anthropic, append them to local storage,
  // update summary. Coalesces concurrent invocations and rate-limits
  // back-to-back calls to POLL_COALESCE_MS.
  async _ingestUpstream() {
    const now = Date.now();
    if (this.pollPromise) return this.pollPromise;
    if (now - this.lastPollMs < POLL_COALESCE_MS) return;

    this.pollPromise = (async () => {
      try {
        const meta = await this.state.storage.get("meta");
        if (!meta) return;
        // `cursor` here is the ISO `created_at` of the most recently
        // ingested event. We pass it to listEvents as `created_at[gt]`,
        // which is Anthropic's documented watermark filter (the
        // events list endpoint does not support id-based pagination).
        //
        // Earlier code stored an event id here. If we see a non-ISO
        // value, drop it and re-ingest from the start of the session
        // (dedupe is keyed on event id so this is safe).
        let cursor = await this.state.storage.get("cursor");
        if (cursor && !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/.test(cursor)) {
          cursor = null;
          await this.state.storage.delete("cursor");
        }

        const events = await listEvents(
          this.env.ANTHROPIC_API_KEY,
          meta.sessionId,
          {
            sinceTs: cursor || null,
            limit: 100,
          },
        );
        // Belt-and-braces: dedupe by event id against what we've
        // already stored. The `created_at[gt]` filter has microsecond
        // resolution but two events written in the same micro-tick
        // could otherwise be re-ingested on overlap.
        const seenIds = (await this.state.storage.get("seen_ids")) || [];
        const seenSet = new Set(seenIds);
        const fresh = events.filter((e) => !seenSet.has(e.id));

        if (fresh.length === 0) {
          // Even with no new events, refresh the session-level usage
          // and status so the summary doesn't go stale on long
          // idle periods.
          await this._refreshSessionMeta(meta.sessionId);
          return;
        }

        const slice = fresh.slice(0, MAX_INGEST_PER_TICK);
        let seq = (await this.state.storage.get("seq")) || 0;
        const summary =
          (await this.state.storage.get("summary")) || _emptySummary();

        const writes = {};
        for (const e of slice) {
          seq += 1;
          writes[EVT_PREFIX + seq] = _normalize(e, seq);
          _projectIntoSummary(summary, e);
          seenSet.add(e.id);
        }
        summary.seq = seq;
        const lastEvent = slice[slice.length - 1];
        const newCursor =
          lastEvent.created_at || lastEvent.processed_at || cursor;
        writes["seq"] = seq;
        writes["cursor"] = newCursor;
        writes["summary"] = summary;
        // Keep the dedupe set bounded — only the last 200 ids matter
        // (events are typed by created_at, so old ids can't reappear).
        const seenArr = Array.from(seenSet);
        writes["seen_ids"] = seenArr.slice(-200);
        await this.state.storage.put(writes);

        // Trim the event ring buffer so storage stays bounded. Drop
        // the oldest events first; clients that fall behind beyond
        // MAX_EVENTS lose history (they should refresh from "since=0").
        if (seq > MAX_EVENTS) {
          const dropTo = seq - MAX_EVENTS;
          const keysToDelete = [];
          for (let i = Math.max(1, dropTo - 200); i <= dropTo; i++) {
            keysToDelete.push(EVT_PREFIX + i);
          }
          if (keysToDelete.length)
            await this.state.storage.delete(keysToDelete);
        }

        // On idle, refresh session metadata (usage) and snapshot the
        // file manifest so the Pager + Mac sync see fresh data
        // without an extra round trip.
        if (summary.status === "idle" || summary.status === "terminated") {
          await this._refreshSessionMeta(meta.sessionId);
          await this._refreshFiles(meta.sessionId);
        }
      } finally {
        this.lastPollMs = Date.now();
        this.pollPromise = null;
      }
    })();
    return this.pollPromise;
  }

  async _refreshSessionMeta(sessionId) {
    try {
      const sess = await getSession(this.env.ANTHROPIC_API_KEY, sessionId);
      if (!sess) return;
      const summary =
        (await this.state.storage.get("summary")) || _emptySummary();
      if (sess.usage) summary.usage = sess.usage;
      if (sess.status) summary.status = sess.status;
      await this.state.storage.put("summary", summary);
    } catch {
      // Non-fatal: this is a freshness optimisation, not a correctness path.
    }
  }

  async _refreshFiles(sessionId) {
    try {
      const data = await listFiles(this.env.ANTHROPIC_API_KEY, sessionId);
      const files = (data?.data || []).map((f) => ({
        id: f.id,
        filename: f.filename,
        size: f.size_bytes ?? f.size ?? null,
        ts: f.created_at || null,
      }));
      const summary =
        (await this.state.storage.get("summary")) || _emptySummary();
      summary.fileCount = files.length;
      await this.state.storage.put("files", files);
      await this.state.storage.put("summary", summary);
    } catch {
      // Files API can lag by seconds after idle; ignore failures.
    }
  }

  async _readEventsSince(since, limit) {
    const fromSeq = Math.max(since + 1, 1);
    const summary =
      (await this.state.storage.get("summary")) || _emptySummary();
    const toSeq = Math.min(summary.seq, fromSeq + limit - 1);
    if (toSeq < fromSeq) return [];
    const keys = [];
    for (let i = fromSeq; i <= toSeq; i++) keys.push(EVT_PREFIX + i);
    const map = await this.state.storage.get(keys);
    const out = [];
    for (let i = fromSeq; i <= toSeq; i++) {
      const ev = map.get(EVT_PREFIX + i);
      if (ev) out.push(ev);
    }
    return out;
  }
}

// ---- Local helpers --------------------------------------------------

function _json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function _bad(message, status = 400) {
  return _json({ error: "bad_request", message }, status);
}
