#!/usr/bin/env python3
"""Claude Code token meter.

Tails the local Claude Code transcripts (~/.claude/projects/**/*.jsonl), turns the
`usage` block of every API response into per-second / per-minute buckets, and serves
a live dashboard on http://127.0.0.1:8765. Standard library only; costs no tokens.
"""
import argparse
import atexit
import getpass
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

# Series order is shared with the dashboard: thinking, text/tool output, fresh input,
# cache write, cache read.
FIELDS = ("think", "out", "in", "cw", "cr")
SEC_KEEP = 2 * 3600          # per-second buckets kept in memory
HISTORY_KEEP = 8 * 86400     # per-minute buckets / message index kept in memory
MAX_SPREAD = 300             # longest window a single response is spread over (s)
ACTIVE_SECONDS = 30          # activity within this many seconds counts as "working"
TOOL_STALE = 3600            # a tool call with no result after this long is ignored
CUM_DAYS = 7                # cumulative totals run from local midnight this many days ago

# range key -> (span seconds, bucket seconds)
RANGES = {
    "1m": (60, 1),
    "5m": (300, 1),
    "15m": (900, 5),
    "1h": (3600, 10),
    "6h": (21600, 60),
    "24h": (86400, 300),
    "7d": (604800, 1800),
}

HERE = Path(__file__).resolve().parent

# Slash-command markers: `<command-name>/model</command-name>` etc; the follow-up
# stdout line for `/model` names the model it switched to, e.g. "Set model to `Sonnet 5`".
CMD_RE = re.compile(r"<command-name>/?([\w-]+)</command-name>")
MODEL_STDOUT_RE = re.compile(r"<local-command-stdout>Set model to `([^`]+)`")


def parse_ts(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def usage_vector(usage):
    def get(key):
        return usage.get(key) or 0

    output = get("output_tokens")
    thinking = min((usage.get("output_tokens_details") or {}).get("thinking_tokens") or 0, output)
    return (
        thinking,
        output - thinking,
        get("input_tokens"),
        get("cache_creation_input_tokens"),
        get("cache_read_input_tokens"),
    )


def message_text(msg):
    """Best-effort plain text of a transcript `user` entry (prompt, tool result, or
    local slash-command echo)."""
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return None


def request_kind(msg):
    """Classify a transcript `user` entry: 'request' if Claude Code sends it to the API,
    'interrupt' if the user cancelled, None for local-only entries (slash command output)."""
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
        return "request"
    text = (message_text(msg) or "").lstrip()
    if text.startswith("[Request interrupted"):
        return "interrupt"
    if not text or text.startswith(("<command-name>", "<local-command-stdout>", "<local-command-caveat>")):
        return None
    return "request"


def _sum_models(model_bucket):
    """Collapse a {model: [5 floats]} bucket (as stored per project) into one vector."""
    out = [0.0] * 5
    if not model_bucket:
        return out
    for vec in model_bucket.values():
        for i in range(5):
            out[i] += vec[i]
    return out


class Meter:
    def __init__(self, roots):
        # One or more `~/.claude/projects`-style roots (e.g. a native one plus a WSL
        # one), so usage from multiple Claude Code install locations on the same
        # machine can be merged into a single dashboard.
        self.roots = [Path(r) for r in roots]
        self.lock = threading.Lock()
        self.sec = {}      # epoch second -> [5 floats]
        self.minute = {}   # epoch minute -> [5 floats]
        self.sec_model = {}      # epoch second -> {model: [5 floats]}
        self.minute_model = {}   # epoch minute -> {model: [5 floats]}
        self.sec_pm = {}      # epoch second -> {project: {model: [5 floats]}}
        self.minute_pm = {}   # epoch minute -> {project: {model: [5 floats]}}
        self.models = []          # model ids, fixed order by first-used timestamp (categorical color slot)
        self.model_first_ts = {}  # model id -> earliest timestamp seen (for ordering self.models)
        self.projects = {}        # project key -> {"label": cwd or dir name, "first_ts": ts}
        self.project_models = {}  # project key -> set of model ids seen in that project
        self.events = []   # [ts, kind, extra, project] for session/model/compact/clear markers
        self.msgs = {}     # message id -> [start, end, vector, model, project]
        self.files = {}    # path -> [offset, partial line, last timestamp, ...]
        self.peak = 0.0
        self.peak_ts = 0
        self.last_activity = 0.0
        self._last_discover = 0.0
        self._last_prune = 0.0

    # --- ingestion -------------------------------------------------------------

    def _spread(self, start, end, vec, sign, model=None, project=None):
        """Distribute a response's tokens uniformly over [start, end)."""
        if not any(vec):
            return
        if end - start < 1:
            start = end - 1
        duration = end - start
        cutoff = time.time() - HISTORY_KEEP
        t = int(start)
        while t < end:
            frac = (min(end, t + 1) - max(start, t)) / duration
            t_bucket = t
            t += 1
            if frac <= 0 or t_bucket < cutoff:
                continue
            add = [v * frac * sign for v in vec]
            sb = self.sec.setdefault(t_bucket, [0.0] * 5)
            mb = self.minute.setdefault(t_bucket // 60, [0.0] * 5)
            for i in range(5):
                sb[i] += add[i]
                mb[i] += add[i]
            if model:
                msb = self.sec_model.setdefault(t_bucket, {}).setdefault(model, [0.0] * 5)
                mmb = self.minute_model.setdefault(t_bucket // 60, {}).setdefault(model, [0.0] * 5)
                for i in range(5):
                    msb[i] += add[i]
                    mmb[i] += add[i]
            if project and model:
                psb = self.sec_pm.setdefault(t_bucket, {}).setdefault(project, {}).setdefault(model, [0.0] * 5)
                pmb = self.minute_pm.setdefault(t_bucket // 60, {}).setdefault(project, {}).setdefault(model, [0.0] * 5)
                for i in range(5):
                    psb[i] += add[i]
                    pmb[i] += add[i]

    def _line(self, raw, state):
        if not raw.strip():
            return
        try:
            entry = json.loads(raw)
        except ValueError:
            return
        if not isinstance(entry, dict):
            return
        ts = parse_ts(entry.get("timestamp"))
        if ts is None:
            return
        project = state[6]
        cwd = entry.get("cwd")
        if cwd and self.projects.get(project, {}).get("label") != cwd:
            self.projects.setdefault(project, {"first_ts": ts})["label"] = cwd
        self.projects.setdefault(project, {"label": project, "first_ts": ts})
        self.projects[project]["first_ts"] = min(self.projects[project]["first_ts"], ts)
        if not state[5]:
            # First entry seen in this transcript file: mark it as a session start.
            self.events.append([ts, "session", None, project])
            state[5] = True
        msg = entry.get("message")
        if (
            entry.get("type") == "assistant"
            and isinstance(msg, dict)
            and isinstance(msg.get("usage"), dict)
            and msg.get("id")
        ):
            # One response is logged as one entry per content block (thinking, text,
            # tool_use), each carrying the response's final usage, so they only reach
            # the file once the response is complete. Count it once, spread from the
            # prompt/tool result that triggered it to its last block.
            vec = usage_vector(msg["usage"])
            model = msg.get("model") or "unknown"
            if model not in self.models:
                self.models.append(model)
            self.model_first_ts[model] = min(self.model_first_ts.get(model, ts), ts)
            self.project_models.setdefault(project, set()).add(model)
            rec = self.msgs.get(msg["id"])
            if rec is None:
                start = state[2] if state[2] and state[2] <= ts else ts - 1
                start = max(start, ts - MAX_SPREAD)
                self._spread(start, ts, vec, +1, model, project)
                self.msgs[msg["id"]] = [start, ts, vec, model, project]
            elif vec != rec[2] or ts > rec[1]:
                self._spread(rec[0], rec[1], rec[2], -1, rec[3], rec[4])
                rec[1] = min(max(ts, rec[1]), rec[0] + MAX_SPREAD)
                rec[2] = vec
                self._spread(rec[0], rec[1], vec, +1, rec[3], rec[4])
            self.last_activity = max(self.last_activity, ts)
            # Response landed: either Claude Code now runs the requested tool, or the turn is over.
            state[3] = "tool" if msg.get("stop_reason") == "tool_use" else None
            state[4] = ts
        elif entry.get("type") == "user":
            # Prompts and tool results mark when the next request was sent. Other entry
            # types (attachments, file history) carry out-of-order timestamps.
            state[2] = max(ts, state[2] or 0)
            kind = request_kind(msg)
            if kind == "interrupt":
                state[3] = None
            elif kind == "request":
                state[3], state[4] = "gen", ts   # a response is being generated right now
            text = message_text(msg) or ""
            cmd = CMD_RE.search(text)
            if cmd:
                name = cmd.group(1).lower()
                if name in ("model", "clear"):
                    self.events.append([ts, name, None, project])
            else:
                switched = MODEL_STDOUT_RE.search(text)
                if switched and self.events and self.events[-1][1] == "model" and ts - self.events[-1][0] < 5:
                    self.events[-1][2] = {"to": switched.group(1)}
        elif entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
            meta = entry.get("compactMetadata") or {}
            self.events.append([ts, "compact", {"pre": meta.get("preTokens"), "post": meta.get("postTokens")}, project])

    def _discover(self, now):
        cutoff = now - HISTORY_KEEP
        for ridx, root in enumerate(self.roots):
            try:
                paths = list(root.rglob("*.jsonl"))
            except OSError:
                continue
            for path in paths:
                if path in self.files:
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                # Untouched for longer than the history window: nothing to show, skip it.
                offset = st.st_size if st.st_mtime < cutoff else 0
                # One Claude Code project directory per distinct cwd; prefix with the root
                # index so two roots (e.g. native + WSL) can't collide on the same name.
                try:
                    project_dir = path.relative_to(root).parts[0]
                except (ValueError, IndexError):
                    project_dir = "unknown"
                project = f"{ridx}:{project_dir}"
                self.projects.setdefault(project, {"label": project_dir, "first_ts": st.st_mtime})
                # offset, partial line, last request time, activity ("gen"/"tool"/None),
                # since, session marked, project key
                self.files[path] = [offset, b"", None, None, 0.0, False, project]

    def poll(self):
        now = time.time()
        with self.lock:
            if now - self._last_discover >= 5:
                self._discover(now)
                self._last_discover = now
            for path, state in list(self.files.items()):
                try:
                    size = path.stat().st_size
                except OSError:
                    del self.files[path]
                    continue
                if size < state[0]:
                    state[0], state[1] = 0, b""   # rewritten; message ids prevent double counting
                if size == state[0]:
                    continue
                try:
                    with open(path, "rb") as fh:
                        fh.seek(state[0])
                        data = fh.read(size - state[0])
                except OSError:
                    continue
                state[0] += len(data)
                lines = (state[1] + data).split(b"\n")
                state[1] = lines.pop()
                for raw in lines:
                    self._line(raw, state)
            if now - self._last_prune >= 30:
                self._prune(now)
                self._last_prune = now

    def _prune(self, now):
        sec_cut = now - SEC_KEEP
        for k in [k for k in self.sec if k < sec_cut]:
            b = self.sec.pop(k)
            if b[0] + b[1] > self.peak:
                self.peak, self.peak_ts = b[0] + b[1], k
            self.sec_model.pop(k, None)
            self.sec_pm.pop(k, None)
        min_cut = (now - HISTORY_KEEP) // 60
        for k in [k for k in self.minute if k < min_cut]:
            del self.minute[k]
            self.minute_model.pop(k, None)
            self.minute_pm.pop(k, None)
        for k in [k for k, r in self.msgs.items() if r[1] < now - HISTORY_KEEP]:
            del self.msgs[k]
        cutoff = now - HISTORY_KEEP
        self.events = [e for e in self.events if e[0] >= cutoff]

    def run(self, interval):
        while True:
            try:
                self.poll()
            except Exception as exc:  # keep the collector alive
                print(f"collector error: {exc}")
            time.sleep(interval)

    # --- queries ---------------------------------------------------------------

    def snapshot(self, range_key, project_key=None):
        span, bucket = RANGES.get(range_key, RANGES["5m"])
        now = time.time()
        end = (int(now) // bucket + 1) * bucket
        start = end - span
        n = span // bucket
        series = [[0.0] * n for _ in FIELDS]
        midnight_dt = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        midnight = midnight_dt.timestamp()
        # Cumulative charts are running totals from a fixed origin, so every range agrees.
        anchor = (midnight_dt - timedelta(days=CUM_DAYS)).timestamp()
        minute_start = start // 60 * 60
        today = [0.0] * 5
        last5h = [0.0] * 5
        cum_base = [0.0] * 5
        with self.lock:
            # A selected project scopes everything to the project/model bucket (which
            # carries both dimensions); no filter uses the plain totals as before.
            proj = project_key if project_key in self.projects else None
            if proj is None:
                sec_all, minute_all = self.sec, self.minute
                msource_all = self.sec_model if bucket < 60 else self.minute_model
            else:
                sec_all = {k: _sum_models(v.get(proj)) for k, v in self.sec_pm.items() if proj in v}
                minute_all = {k: _sum_models(v.get(proj)) for k, v in self.minute_pm.items() if proj in v}
                pmsource = self.sec_pm if bucket < 60 else self.minute_pm
                msource_all = {k: v.get(proj, {}) for k, v in pmsource.items() if proj in v}
            if bucket < 60:
                source, unit, lo, hi = sec_all, 1, start, end
            else:
                source, unit, lo, hi = minute_all, 60, start // 60, end // 60
            for key in range(lo, hi):
                b = source.get(key)
                if b:
                    idx = (key * unit - start) // bucket
                    for i in range(5):
                        series[i][idx] += b[i]
            for key, b in minute_all.items():
                t = key * 60
                if t >= midnight:
                    for i in range(5):
                        today[i] += b[i]
                if t >= now - 5 * 3600:
                    for i in range(5):
                        last5h[i] += b[i]
                if anchor <= t < minute_start:
                    for i in range(5):
                        cum_base[i] += b[i]
            for t in range(minute_start, start):  # partial minute before a 1 s / 5 s / 10 s range
                b = sec_all.get(t)
                if b:
                    for i in range(5):
                        cum_base[i] += b[i]
            # Per-model output/input rate, same bucketing as the totals above.
            series_model = {}
            for key in range(lo, hi):
                mb = msource_all.get(key)
                if not mb:
                    continue
                idx = (key * unit - start) // bucket
                for model, vec in mb.items():
                    sm = series_model.setdefault(model, {"out": [0.0] * n, "in": [0.0] * n})
                    sm["out"][idx] += vec[0] + vec[1]
                    sm["in"][idx] += vec[2] + vec[3] + vec[4]
            # One point per API request: total prompt size (fresh + cache write + cache read).
            requests = sorted(
                (
                    {"t": round(rec[1], 1), "ctx": round(rec[2][2] + rec[2][3] + rec[2][4]), "model": rec[3]}
                    for rec in self.msgs.values()
                    if start <= rec[1] <= now + 1 and (proj is None or rec[4] == proj)
                ),
                key=lambda r: r["t"],
            )
            events = sorted(
                (
                    {"t": e[0], "kind": e[1], "extra": e[2]}
                    for e in self.events
                    if start <= e[0] <= now + 1 and (proj is None or e[3] == proj)
                ),
                key=lambda e: e["t"],
            )
            activity = [
                {"kind": st[3], "since": st[4]}
                for st in self.files.values()
                if st[3] and now - st[4] < (MAX_SPREAD if st[3] == "gen" else TOOL_STALE)
                and (proj is None or st[6] == proj)
            ]
            # 7-day peak: pruned seconds are folded into self.peak; recent ones are live.
            # (The fold-in is global only, so a project-filtered peak only reflects the
            # currently-held ~2h of seconds, not the full 7-day history.)
            peak, peak_ts = (self.peak, self.peak_ts) if proj is None else (0.0, 0)
            for k, b in sec_all.items():
                if b[0] + b[1] > peak:
                    peak, peak_ts = b[0] + b[1], k
            models_in_scope = self.project_models.get(proj, self.models) if proj else self.models
            payload = {
                "user": USER,
                "now": now,
                "start": start,
                "bucket": bucket,
                "range": range_key if range_key in RANGES else "5m",
                "series": {f: [round(max(v, 0.0), 2) for v in series[i]] for i, f in enumerate(FIELDS)},
                "today": {f: round(max(today[i], 0.0)) for i, f in enumerate(FIELDS)},
                "last5h": {f: round(max(last5h[i], 0.0)) for i, f in enumerate(FIELDS)},
                "cum_base": {f: round(max(cum_base[i], 0.0), 2) for i, f in enumerate(FIELDS)},
                "cum_anchor": anchor,
                "models": sorted(
                    (m for m in self.models if m in models_in_scope), key=lambda m: self.model_first_ts.get(m, 0)
                ),
                "series_model": {
                    m: {"out": [round(v, 2) for v in s["out"]], "in": [round(v, 2) for v in s["in"]]}
                    for m, s in series_model.items()
                },
                "requests": requests,
                "events": events,
                "activity": activity,
                "peak": round(peak, 1),
                "peak_ts": peak_ts,
                "last_activity": self.last_activity,
                "active": now - self.last_activity < ACTIVE_SECONDS,
                "files": len(self.files),
                "project": proj,
                "projects": sorted(
                    (
                        {"key": key, "label": info["label"]}
                        for key, info in self.projects.items()
                    ),
                    key=lambda p: p["label"].lower(),
                ),
            }
        return payload


class Handler(BaseHTTPRequestHandler):
    meter = None

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            try:
                body = (HERE / "dashboard.html").read_bytes()
            except OSError:
                return self._send(500, b"dashboard.html missing", "text/plain")
            return self._send(200, body, "text/html; charset=utf-8")
        if url.path == "/api/data":
            qs = parse_qs(url.query)
            key = qs.get("range", ["5m"])[0]
            project = qs.get("project", [None])[0]
            body = json.dumps(self.meter.snapshot(key, project), separators=(",", ":")).encode()
            return self._send(200, body, "application/json")
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/api/launch-remote":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                req = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                req = {}
            try:
                port = int(req.get("port", 8766))
            except (TypeError, ValueError):
                return self._send(400, json.dumps({"ok": False, "message": "bad port"}).encode(), "application/json")
            result = launch_remote(port, confirm=bool(req.get("confirm")))
            body = json.dumps(result).encode()
            return self._send(200 if result["ok"] else 500, body, "application/json")
        self._send(404, b"not found", "text/plain")

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Lets a dashboard served by one meter (e.g. native Windows) fetch /api/data from
        # another meter on a different port (e.g. one running inside WSL) for the Local/
        # Remote source toggle, without either meter polling the other's filesystem.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def default_projects_dir():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "projects")


def current_user():
    try:
        return getpass.getuser()
    except (KeyError, OSError):  # e.g. a container UID with no passwd entry
        return os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"


USER = current_user()


def _win_to_wsl_path(path):
    """`C:\\Users\\me\\...\\token_meter.py` -> `/mnt/c/Users/me/.../token_meter.py`."""
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", str(path))
    if not m:
        return None
    drive, rest = m.group(1).lower(), m.group(2).replace("\\", "/")
    return f"/mnt/{drive}/{rest}"


def _already_listening(port, timeout=1.0):
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/data?range=1m", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def launch_remote(port, confirm=False):
    """Start a second `token_meter.py` inside WSL, for the dashboard's Remote toggle.

    Only makes sense from the Windows side: WSL already sees its own native
    ~/.claude/projects, so this is purely "spin up the WSL counterpart on demand"
    rather than anything symmetric. Runs the same script this process is running
    (translated to its /mnt/<drive>/... path), so it always matches this checkout.

    `confirm=False` (the default) is a dry run: it reports what *would* run, without
    running it, so the dashboard can show the exact command in a confirm dialog
    before the caller comes back with `confirm=True` to actually launch it."""
    if platform.system() != "Windows":
        return {"ok": False, "message": "Launching a WSL meter is only supported when this meter runs on Windows."}
    if not (shutil.which("wsl.exe") or shutil.which("wsl")):
        return {"ok": False, "message": "wsl.exe not found on PATH."}
    if _already_listening(port):
        # Distinguish "we started this and it's still alive" (safe to claim/clean up
        # on our own exit) from "something else is answering there" (someone's own
        # WSL session, or a leftover from before this process restarted — leave it
        # alone either way, but the dashboard can say which it is).
        return {"ok": True, "message": "already running", "running": True, "launched_by_us": port in _launched_remote_ports}
    wsl_path = _win_to_wsl_path((HERE / "token_meter.py").resolve())
    if not wsl_path:
        return {"ok": False, "message": "Could not translate this script's path to a WSL path (not on a drive letter?)."}
    command = ["wsl.exe", "-e", "python3", wsl_path, "--port", str(port), "--no-browser"]
    if not confirm:
        return {"ok": True, "message": "ready", "running": False, "command": command}
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except OSError as exc:
        return {"ok": False, "message": f"Failed to launch: {exc}"}
    # Remember it so this process's own shutdown (Ctrl+C, atexit) can stop it too —
    # `wsl.exe -e ...` detaches once launched, so a plain Ctrl+C here otherwise leaves
    # the WSL-side python3 running forever, invisible from the Windows side.
    _launched_remote_ports.add(port)
    return {"ok": True, "message": "launching", "running": False, "command": command}


_launched_remote_ports = set()   # ports this process itself launched a WSL meter on


def stop_launched_remotes():
    for port in list(_launched_remote_ports):
        try:
            # Matches this exact invocation (script + port), so it can't catch an
            # unrelated meter someone started by hand on a different port.
            subprocess.run(
                ["wsl.exe", "-e", "pkill", "-f", f"token_meter.py --port {port} "],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        _launched_remote_ports.discard(port)


def main():
    parser = argparse.ArgumentParser(description="Live Claude Code token meter")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--projects",
        action="append",
        help="Claude Code projects folder (repeatable, e.g. to add a WSL "
        "\\\\wsl.localhost\\<distro>\\home\\<user>\\.claude\\projects path alongside the "
        "native one); defaults to the local ~/.claude/projects",
    )
    parser.add_argument("--interval", type=float, default=1.0, help="transcript poll interval (s)")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    roots = args.projects or [default_projects_dir()]

    meter = Meter(roots)
    started = time.time()
    meter.poll()  # initial backfill of the history window
    print(f"Scanned {len(meter.files)} transcripts in {time.time() - started:.2f}s ({', '.join(roots)})")
    threading.Thread(target=meter.run, args=(args.interval,), daemon=True).start()

    Handler.meter = meter
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Token meter for {USER}: {url}  (Ctrl+C to stop)")
    # Also stop any WSL meter this process itself launched via the Remote toggle —
    # atexit covers Ctrl+C, a normal return, and an unhandled exception alike.
    atexit.register(stop_launched_remotes)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
