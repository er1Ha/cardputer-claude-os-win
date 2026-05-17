"""Receive character packs pushed from the desktop — DISABLED.

Protocol (excerpt from REFERENCE.md):
    {"cmd":"char_begin","name":"luna"}
    {"cmd":"file","path":"idle.png","size":1234}
    {"cmd":"chunk","data":"<base64>"}              (repeat)
    {"cmd":"file_end","crc32":"..."}
    {"cmd":"char_begin"} ... next file ...
    {"cmd":"char_end"}

**This receiver is gated off on the UIFlow 2.0 build.** The BLE link
is unauthenticated (see ``buddy_ble.py`` docstring), so any central
in range could otherwise drop arbitrary files into
``/flash/buddy/chars/<name>/``. Path traversal is blocked by
``_safe_segment`` so an attacker cannot overwrite ``/flash/main.py``
or other system files, but the residual exposure (flash-fill DoS,
latent payload staging if any future code adds chars/ to sys.path)
is enough that we refuse the whole feature until the link is
authenticated.

``CharReceiver.handle`` returns a refusal ack for every file-push
command. The dispatch internals below stay in place so a future
build with link encryption (or an application-layer auth handshake)
can re-enable file push by removing the early return at the top of
``handle``.

Sandboxing rules that still apply when this is re-enabled:
- Total push <= ``MAX_PACK_BYTES``. If a single char pack is larger,
  the desktop lied about the size — reject.
- No "../" or absolute paths. Strip any backslashes. Everything
  lands under /flash/buddy/chars/<name>/.
- A file is only committed (renamed from .part) when file_end
  arrives. If the transfer breaks mid-stream we clean up .part
  files next boot.
"""

try:
    import ubinascii as _b64
except ImportError:
    import binascii as _b64  # type: ignore

try:
    import os
except ImportError:
    os = None

try:
    import uzlib as _zlib  # noqa: F401 (reserved for future crc32)
except ImportError:
    try:
        import zlib as _zlib  # type: ignore  # noqa: F401
    except ImportError:
        _zlib = None

CHARS_ROOT = "/flash/buddy/chars"
# Belt-and-suspenders ceiling. Even with handle() rejecting up front,
# anyone re-enabling the receiver later without thinking about the cap
# is forced to confront it: 0 bytes means literally nothing accepted.
# Set to a sane positive value (e.g. 1_800_000) when re-enabling.
MAX_PACK_BYTES = 0


def _safe_segment(p: str) -> str:
    p = p.replace("\\", "/")
    # Drop leading slashes so the path is always relative
    while p.startswith("/"):
        p = p[1:]
    parts = []
    for seg in p.split("/"):
        if seg in ("", ".", ".."):
            continue
        parts.append(seg)
    return "/".join(parts)


def _ensure_dir(path: str):
    if os is None:
        return
    pieces = path.strip("/").split("/")
    cur = ""
    for p in pieces:
        cur = cur + "/" + p
        try:
            os.mkdir(cur)
        except OSError:
            pass  # already exists


class CharReceiver:
    def __init__(self):
        self._current_char = None      # name of the char currently in flight
        self._current_file = None      # {"path", "size", "written", "fp"}
        self._bytes_this_pack = 0

    def _close_fp(self):
        """Close any in-flight file handle and unlink its .part.

        Called whenever the dispatcher transitions out of a "file in
        flight" state — start of a new file, start of a new char, end
        of a char, or any error path. Without this, an out-of-sequence
        message (host sends `file` again before sending `file_end`,
        or sends `char_end` while a file is still in flight) leaks the
        previous file's open handle until the next reboot, and the
        truncated .part it left behind would be picked up by
        sweep_partials() — but only after a reboot, not now. So we
        clean up immediately too. Idempotent; safe to call on a clean
        slate.
        """
        f = self._current_file
        if f is None:
            return
        try:
            f["fp"].close()
        except OSError:
            pass
        # Best-effort: drop the .part so a subsequent run doesn't see
        # a half-written blob. Sweep_partials will get the rest at
        # next boot if this fails.
        if os is not None:
            try:
                os.remove(f["path"] + ".part")
            except OSError:
                pass
        self._current_file = None

    def handle(self, msg: dict) -> dict:
        """Dispatch one decoded JSON message. Returns an ack dict or {}.

        File-push is refused on the unauthenticated UIFlow 2.0 link;
        every char_*/file/chunk/file_end command gets a structured
        refusal so the desktop can render a clear error rather than
        silently retrying. To re-enable on a build with link auth,
        remove the early-return block below.
        """
        cmd = msg.get("cmd")
        if cmd in ("char_begin", "file", "chunk", "file_end", "char_end"):
            return {
                "ack": cmd,
                "ok": False,
                "err": "file push disabled on unauthenticated link",
            }
        # Unreachable on this build (handle() is only called for the
        # gated cmds above), but kept for parity with future re-enable:
        return {}

    def _begin_char(self, msg):
        # Close any in-flight file the previous char left open before
        # we move on — without this, char_begin during a transfer
        # leaks the prior fp.
        self._close_fp()
        name = _safe_segment(msg.get("name", ""))
        if not name:
            return {"ack": "char_begin", "ok": False, "err": "empty name"}
        self._current_char = name
        self._bytes_this_pack = 0
        _ensure_dir("{}/{}".format(CHARS_ROOT, name))
        return {"ack": "char_begin", "ok": True, "name": name}

    def _begin_file(self, msg):
        # Out-of-sequence file: a previous file is still open. Close
        # and discard before opening the new one so the prior fp
        # doesn't leak and the prior .part doesn't sit around looking
        # like a valid (just slightly truncated) blob.
        self._close_fp()
        if self._current_char is None:
            return {"ack": "file", "ok": False, "err": "no char context"}
        rel = _safe_segment(msg.get("path", ""))
        if not rel:
            return {"ack": "file", "ok": False, "err": "empty path"}
        size = int(msg.get("size", 0))
        # Declared size must be non-negative AND fit within the global
        # pack ceiling. The actually-received-bytes check happens in
        # _chunk; this is the early reject for hosts that announce a
        # too-large file up front.
        if size < 0 or self._bytes_this_pack + size > MAX_PACK_BYTES:
            return {"ack": "file", "ok": False, "err": "pack too large"}
        full = "{}/{}/{}".format(CHARS_ROOT, self._current_char, rel)
        # Build parent dirs as needed so nested layouts (fonts/, anims/)
        # work without the desktop having to send per-dir commands.
        parent = full.rsplit("/", 1)[0]
        _ensure_dir(parent)
        try:
            fp = open(full + ".part", "wb")
        except OSError as e:
            return {"ack": "file", "ok": False, "err": str(e)}
        self._current_file = {"path": full, "size": size, "written": 0, "fp": fp}
        return {"ack": "file", "ok": True, "path": full}

    def _chunk(self, msg):
        f = self._current_file
        if f is None:
            return {"ack": "chunk", "ok": False, "err": "no file"}
        data_b64 = msg.get("data", "")
        try:
            data = _b64.a2b_base64(data_b64)
        except Exception as e:
            return {"ack": "chunk", "ok": False, "err": "b64: " + str(e)}
        # Bound by ACTUAL bytes, not just declared size. A host that
        # declared size=0 (or any small value) and tried to stream
        # forever would otherwise pass the begin-time check and fill
        # the file system. We close on overflow so the partial blob
        # doesn't sit around between connections.
        n = len(data)
        if f["written"] + n > f["size"]:
            self._close_fp()
            return {"ack": "chunk", "ok": False, "err": "exceeds declared size"}
        if self._bytes_this_pack + n > MAX_PACK_BYTES:
            self._close_fp()
            return {"ack": "chunk", "ok": False, "err": "pack too large"}
        try:
            f["fp"].write(data)
        except OSError as e:
            self._close_fp()
            return {"ack": "chunk", "ok": False, "err": str(e)}
        f["written"] += n
        self._bytes_this_pack += n
        # We don't ack every chunk; the desktop relies on TCP-style
        # pacing from its own writes and only cares about the final
        # file_end ack. Return empty to keep the wire quiet.
        return {}

    def _end_file(self, msg):
        f = self._current_file
        if f is None:
            return {"ack": "file_end", "ok": False, "err": "no file"}
        # Truncated transfer: written < declared size. Reject and
        # remove the .part — partials never get atomically renamed
        # to the final path, so a partial transfer cannot pass for
        # a complete one. The previous behavior was "warn and keep",
        # which let a host underdeclare-then-stop produce a file the
        # device treated as valid.
        if f["size"] and f["written"] != f["size"]:
            self._close_fp()
            return {
                "ack": "file_end",
                "ok": False,
                "err": "size mismatch",
                "written": f["written"],
                "declared": f["size"],
            }
        try:
            f["fp"].close()
        except OSError:
            pass
        # Atomic-ish rename: the .part exists while writing, the
        # clean filename only after end. A crash mid-transfer leaves
        # a .part file which the next boot can sweep.
        if os is not None:
            try:
                os.rename(f["path"] + ".part", f["path"])
            except OSError as e:
                self._current_file = None
                return {"ack": "file_end", "ok": False, "err": str(e)}
        result = {
            "ack": "file_end",
            "ok": True,
            "path": f["path"],
            "written": f["written"],
        }
        self._current_file = None
        return result

    def _end_char(self, _msg):
        # Close any file that was still in flight — char_end without
        # a preceding file_end is a host-side bug, but we shouldn't
        # leak the fp because of it.
        self._close_fp()
        name = self._current_char
        self._current_char = None
        return {"ack": "char_end", "ok": True, "name": name or "", "bytes": self._bytes_this_pack}


def sweep_partials():
    """Delete leftover .part files from an interrupted transfer.

    Call at startup. These are always invalid (the rename never
    happened) so there's no data to rescue — just unlink them.
    """
    if os is None:
        return
    try:
        if "buddy" not in os.listdir("/flash"):
            return
    except OSError:
        return
    stack = [CHARS_ROOT]
    while stack:
        d = stack.pop()
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for name in entries:
            p = d + "/" + name
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st[0] & 0x4000:  # directory
                stack.append(p)
            elif name.endswith(".part"):
                try:
                    os.remove(p)
                except OSError:
                    pass
