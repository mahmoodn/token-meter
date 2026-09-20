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
import math
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
WEB_SEARCH_COST = 0.01       # $/request ($10 per 1k), billed per call rather than per token
CALIBRATE_MIN_USD = 1.0      # Claude Code must have billed this much on a model before its ratio overrides PRICING
SYNTHETIC_MODEL = "<synthetic>"   # Claude Code's placeholder "model" for messages it wrote itself, not the API

# range key -> (span seconds, bucket seconds)
RANGES = {
    "1m": (60, 1),
    "15m": (900, 5),
    "1h": (3600, 10),
    "5h": (18000, 60),  # matches Claude's 5-hour session rate-limit window (see CLAUDE.md)
    "24h": (86400, 300),
    "7d": (604800, 1800),
}

# $/MTok, per model: base input, output (thinking + text/tool calls both bill at this
# rate), cache write, cache read. Source: platform.claude.com/docs/en/about-claude/
# pricing (checked 2026-09-18).
#
# This is a *seed*, not the source of truth, and nobody needs to keep it current.
# Meter._rates() calibrates it against Claude Code's own accounting, which Claude Code
# writes into every transcript as `type: "cost-state"` entries priced with the rates
# built into its own release — so once a model has real history here, its rate comes
# from that, and a stale entry (or a missing one) is corrected at runtime with no edit,
# no push and no pull. The table is what you see for a model with under $1 of history
# on this machine, or on a Claude Code too old to write cost-state. Prices as of the
# date above; it's still worth bumping now and then so a fresh install starts close.
#
# `cw` is the **1-hour** TTL cache-write rate (2x base input), not the 5-minute one
# (1.25x). This started as the opposite assumption and was measurably wrong: every
# cache write in every transcript on this machine (7001 of 7001, across 5 models)
# carries `usage.cache_creation.ephemeral_1h_input_tokens`, i.e. Claude Code caches
# at 1h. Checked against the cost-state totals of every session on this machine
# (2026-09-20, 11 sessions): at the 5m rate this table lands 11.7% under Claude Code's
# number, at the 1h rate 0.8% over (Haiku exact, Sonnet 0.2%, Opus 2.5%). That Opus
# residual is one session (10 days old; +6.4% on Opus) — every other session matches
# to 0.00%, and the cause isn't known. If a mixed workload ever matters, the
# per-response 5m/1h split is right there in `usage.cache_creation` — it would need a
# 6th entry in FIELDS to carry through the buckets.
#
# Not counted: web search, billed per request ($10/1k) rather than per token. It's in
# the transcript as `usage.server_tool_use.web_search_requests` if it ever matters —
# it accounts for the whole residual on search-heavy models (29 searches = $0.29).
#
# A model that is neither here nor in Claude Code's cost records (or "<synthetic>",
# its placeholder for non-model-generated entries) costs $0 rather than erroring.
# This is an estimate of equivalent pay-as-you-go API cost for comparison, not a real
# bill — Claude Pro/Max subscriptions aren't metered per token.
PRICING = {
    "claude-opus-5":   {"in": 5.00,  "out": 25.00, "cw": 10.00, "cr": 0.50},
    "claude-opus-4-8": {"in": 5.00,  "out": 25.00, "cw": 10.00, "cr": 0.50},
    "claude-sonnet-5": {"in": 2.00,  "out": 10.00, "cw": 4.00,  "cr": 0.20},
    "claude-fable-5":  {"in": 10.00, "out": 50.00, "cw": 20.00, "cr": 1.00},
    # Claude Code runs small background calls (conversation titles and the like) on
    # Haiku; cheap, but it was silently free before it had an entry.
    "claude-haiku-4-5": {"in": 1.00, "out": 5.00,  "cw": 2.00,  "cr": 0.10},
}
# Precomputed per-token $ rate in FIELDS order (think, out, in, cw, cr) — thinking
# bills as output, so it reuses the "out" rate.
PRICE_VEC = {
    m: (p["out"] / 1e6, p["out"] / 1e6, p["in"] / 1e6, p["cw"] / 1e6, p["cr"] / 1e6)
    for m, p in PRICING.items()
}
# Every model in PRICING prices output, cache write and cache read at the same multiples
# of its base input rate. Used only to price a model the table has never heard of, from
# nothing but its total cost in Claude Code's records (see Meter._rates) — that has to
# assume some shape, and this is the one all five listed models share.
PRICE_SHAPE = {"out": 5.0, "cw": 2.0, "cr": 0.1}

# Context window size (tokens), per model — the only thing the "context filling up"
# hint needs: compare a request's ctx (fresh input + cache write + cache read, already
# computed for the per-request chart) against this. NOT "the standard 200k window":
# measured against every transcript on this machine, prompts routinely run far past
# 200k on all three models in regular use (830k Sonnet 5, 747k Opus 5, 685k Opus 4.8;
# ~2000 requests over 200k), i.e. Claude Code is using the 1M long-context mode. A
# 200k table therefore reported ">100% of the window" as an ordinary state, which is
# exactly the cry-wolf failure that makes a warning worth ignoring. Nothing in the
# transcript states the active window, so this errs large deliberately: the cost is
# that an account *without* long-context gets warned later than it could, which is
# the better failure. A model with no entry here just doesn't get the hint, same
# fallback style as PRICING. See also the >100% backstop in renderHints().
CONTEXT_LIMITS = {
    "claude-opus-5":   1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-fable-5":  1_000_000,
}


def _model_key(model):
    """Table lookup key for a model id. Claude Code reports some models with a release
    date suffix (`claude-haiku-4-5-20251001`) and some without (`claude-sonnet-5`);
    without stripping it, a dated id silently misses PRICING and prices as free."""
    return re.sub(r"-\d{8}$", "", model or "")


def _bucket_cost(model_bucket, rates):
    """$ cost of a {model: [5 floats]} bucket (as stored per second/minute, optionally
    per project), summed across whichever models are priced. `rates` is Meter._rates()'s
    {model key: per-token $ in FIELDS order}. Unpriced models (unknown ids,
    "<synthetic>") contribute $0 rather than raising."""
    if not model_bucket:
        return 0.0
    total = 0.0
    for model, vec in model_bucket.items():
        rate = rates.get(_model_key(model))
        if rate:
            total += sum(v * r for v, r in zip(vec, rate))
    return total


def _num(value):
    """A finite number from a transcript field, else 0 — Claude Code's cost-state is
    read on faith, and one odd value shouldn't take the whole snapshot down."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0
    return value


HERE = Path(__file__).resolve().parent

# Slash-command markers: `<command-name>/model</command-name>` etc; the follow-up
# stdout line for `/model` names the model it switched to, e.g. "Set model to `Sonnet 5`".
CMD_RE = re.compile(r"<command-name>/?([\w-]+)</command-name>")
# Only another meter's dashboard on this machine may read /api/data cross-origin or
# POST here; see Handler._cors_origin.
LOCAL_ORIGIN_RE = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")
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
    def __init__(self, roots, subscription_price=22.0):
        # One or more `~/.claude/projects`-style roots (e.g. a native one plus a WSL
        # one), so usage from multiple Claude Code install locations on the same
        # machine can be merged into a single dashboard.
        self.roots = [Path(r) for r in roots]
        # Reference line for the estimated-cost chart — your flat subscription price,
        # purely for comparison ("am I getting more than $X of value"), not a budget.
        self.subscription_price = subscription_price
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
        # transcript path -> {model: (token cost $, out, in, cache write, cache read)}, the
        # latest of Claude Code's own per-model accounting for that session (see _rates)
        self.cost_state = {}
        self.files = {}    # path -> [offset, partial line, last timestamp, ...]
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
        if entry.get("type") == "cost-state" and isinstance(entry.get("modelUsage"), dict):
            # Claude Code's own running cost for this session, priced with the rates
            # built into *its* release — so it tracks Anthropic's current prices without
            # this tool ever making a request. Cumulative and last-one-wins per session,
            # which is why it's keyed by transcript rather than accumulated. Web search
            # bills per request, not per token, so it's taken out of the cost here: what
            # is kept is the cost of the tokens alone, which is all the buckets hold.
            # Checked before the timestamp guard below: these entries carry no
            # timestamp, so anything after that guard never sees them.
            self.cost_state[state[7]] = {
                model: (
                    _num(u.get("costUSD")) - _num(u.get("webSearchRequests")) * WEB_SEARCH_COST,
                    _num(u.get("outputTokens")),
                    _num(u.get("inputTokens")),
                    _num(u.get("cacheCreationInputTokens")),
                    _num(u.get("cacheReadInputTokens")),
                )
                for model, u in entry["modelUsage"].items()
                if isinstance(u, dict)
            }
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
            # Claude Code writes its own messages ("No response requested.", API errors,
            # "Login expired") as assistant entries with model "<synthetic>" and no
            # tokens at all — 5 of 5 in every transcript on this machine. It isn't a
            # model: counted as one it got a legend entry whose name the browser then
            # swallowed as an HTML tag, a colour slot, a zero-height point on the context
            # chart, and it could become the "latest request" and silence the context
            # hint. Such a message still ends the request in flight, so the activity
            # bookkeeping below applies to it; only the token/model accounting is skipped.
            # (Only when it really is empty: a synthetic message with tokens would be
            # dropped silently, so that case stays visible instead.)
            if not (model == SYNTHETIC_MODEL and not any(vec)):
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
                # since, session marked, project key, path, last modified
                self.files[path] = [offset, b"", None, None, 0.0, False, project, path, st.st_mtime]

    def poll(self):
        now = time.time()
        with self.lock:
            if now - self._last_discover >= 5:
                self._discover(now)
                self._last_discover = now
            for path, state in list(self.files.items()):
                try:
                    st = path.stat()
                except OSError:
                    del self.files[path]
                    continue
                size = st.st_size
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
                state[8] = st.st_mtime
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
            del self.sec[k]
            self.sec_model.pop(k, None)
            self.sec_pm.pop(k, None)
        min_cut = (now - HISTORY_KEEP) // 60
        for k in [k for k in self.minute if k < min_cut]:
            del self.minute[k]
            self.minute_model.pop(k, None)
            self.minute_pm.pop(k, None)
        cutoff = now - HISTORY_KEEP
        for k in [k for k, r in self.msgs.items() if r[1] < cutoff]:
            del self.msgs[k]
        self.events = [e for e in self.events if e[0] >= cutoff]
        # Sessions untouched for the whole retention window stop informing the price
        # calibration, the same as they stop informing the charts — otherwise a meter left
        # running for weeks would keep averaging in last month's rates after a price change.
        for path in [p for p in self.cost_state if p not in self.files or self.files[p][8] < cutoff]:
            del self.cost_state[path]
        # A model with no request left in the window drops out too, along with each
        # project's copy of it. This list only ever grew before, so a model you stopped
        # using last week kept its legend entry and — worse — its colour slot, pushing a
        # model you use today onto a colour someone else already had. Judged by the
        # message index, which is pruned to the same horizon as the buckets.
        live, live_pm = set(), {}
        for rec in self.msgs.values():
            live.add(rec[3])
            live_pm.setdefault(rec[4], set()).add(rec[3])
        self.models = [m for m in self.models if m in live]
        for m in [m for m in self.model_first_ts if m not in live]:
            del self.model_first_ts[m]
        self.project_models = {p: live_pm.get(p, set()) for p in self.project_models}

    def run(self, interval):
        while True:
            try:
                self.poll()
            except Exception as exc:  # keep the collector alive
                print(f"collector error: {exc}")
            time.sleep(interval)

    # --- queries ---------------------------------------------------------------

    def _rates(self):
        """Effective per-token $ rates (FIELDS order) for every priced model, plus where
        each one came from: `({model key: rates}, {model key: note})`. Caller holds the lock.

        PRICING is only a seed. Claude Code prices each session with the rates compiled
        into its own release and writes the result to the transcript, so those records
        are a local, network-free feed of *current* pricing that updates whenever Claude
        Code does. For every model with enough history in them:

          - listed in PRICING: scale its rates by (Claude Code's cost) / (the table's cost
            for the same tokens). A ratio needs no assumption about how the pricing is
            structured, so it survives a change to the cache multiples, not just the base
            rate. One scalar can't fix a bucket whose token mix differs from the model's
            overall mix, so totals come out right and individual buckets approximately.
          - not listed: derive the base input rate from Claude Code's cost outright,
            assuming PRICE_SHAPE. Without this a new model prices as $0 until someone
            edits the table.

        Anything else keeps its PRICING rates ("table"), i.e. nothing has checked them.
        Spans each session's whole life rather than the retention window; that is fine for
        a ratio, since numerator and denominator cover the same tokens."""
        totals = {}
        for usage in self.cost_state.values():
            for model, vals in usage.items():
                agg = totals.setdefault(_model_key(model), [0.0] * 5)
                for i, v in enumerate(vals):
                    agg[i] += v
        rates = dict(PRICE_VEC)
        notes = {key: {"source": "table"} for key in PRICE_VEC}
        for key, (theirs, out, inp, cw, cr) in totals.items():
            if theirs <= 0:
                continue
            seed = PRICE_VEC.get(key)
            if seed:
                ours = out * seed[1] + inp * seed[2] + cw * seed[3] + cr * seed[4]
                if theirs < CALIBRATE_MIN_USD or ours <= 0:
                    continue
                factor = theirs / ours
                rates[key] = tuple(r * factor for r in seed)
                notes[key] = {"source": "calibrated", "factor": factor}
            else:
                # Everything expressed in units of base input tokens, so the cost divides out.
                units = inp + out * PRICE_SHAPE["out"] + cw * PRICE_SHAPE["cw"] + cr * PRICE_SHAPE["cr"]
                if units <= 0:
                    continue
                base = theirs / units
                rates[key] = (
                    base * PRICE_SHAPE["out"], base * PRICE_SHAPE["out"], base,
                    base * PRICE_SHAPE["cw"], base * PRICE_SHAPE["cr"],
                )
                notes[key] = {"source": "derived", "input_per_mtok": base * 1e6}
        return rates, notes

    def snapshot(self, range_key, project_key=None):
        span, bucket = RANGES.get(range_key, RANGES["1h"])
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
        cum_base = [0.0] * 5
        cost_cum_base = 0.0
        cache15_cw = cache15_cr = 0.0
        with self.lock:
            rates, rate_notes = self._rates()
            # A selected project scopes everything to the project/model bucket (which
            # carries both dimensions); no filter uses the plain totals as before.
            proj = project_key if project_key in self.projects else None
            if proj is None:
                sec_all, minute_all = self.sec, self.minute
                sec_model_all, minute_model_all = self.sec_model, self.minute_model
            else:
                sec_all = {k: _sum_models(v.get(proj)) for k, v in self.sec_pm.items() if proj in v}
                minute_all = {k: _sum_models(v.get(proj)) for k, v in self.minute_pm.items() if proj in v}
                sec_model_all = {k: v.get(proj, {}) for k, v in self.sec_pm.items() if proj in v}
                minute_model_all = {k: v.get(proj, {}) for k, v in self.minute_pm.items() if proj in v}
            # Cost only needs the model dimension (rate varies by model, not by token
            # type within a model), so it's derived from the *_model sources rather than
            # tracked as its own bucket family.
            msource_all = sec_model_all if bucket < 60 else minute_model_all
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
                mb = minute_model_all.get(key)
                if t >= midnight:
                    for i in range(5):
                        today[i] += b[i]
                if anchor <= t < minute_start:
                    for i in range(5):
                        cum_base[i] += b[i]
                    cost_cum_base += _bucket_cost(mb, rates)
                if t >= now - 900:
                    # Cache write vs. cache read over a short, fixed recent window — a
                    # cache-churn hint (see CLAUDE.md) rather than a scoped chart series,
                    # so it doesn't need to track its own bucket range independently.
                    cache15_cw += b[3]
                    cache15_cr += b[4]
            for t in range(minute_start, start):  # partial minute before a 1 s / 5 s / 10 s range
                b = sec_all.get(t)
                if b:
                    for i in range(5):
                        cum_base[i] += b[i]
                    cost_cum_base += _bucket_cost(sec_model_all.get(t), rates)
            # Per-model output/input rate, and estimated cost, same bucketing as above.
            series_model = {}
            cost_series = [0.0] * n
            for key in range(lo, hi):
                mb = msource_all.get(key)
                if not mb:
                    continue
                idx = (key * unit - start) // bucket
                for model, vec in mb.items():
                    sm = series_model.setdefault(model, {"out": [0.0] * n, "in": [0.0] * n})
                    sm["out"][idx] += vec[0] + vec[1]
                    sm["in"][idx] += vec[2] + vec[3] + vec[4]
                cost_series[idx] += _bucket_cost(mb, rates)
            # One point per API request: total prompt size (fresh + cache write + cache read).
            requests = sorted(
                (
                    {"t": round(rec[1], 1), "ctx": round(rec[2][2] + rec[2][3] + rec[2][4]), "model": rec[3]}
                    for rec in self.msgs.values()
                    if start <= rec[1] <= now + 1 and (proj is None or rec[4] == proj)
                ),
                key=lambda r: r["t"],
            )
            # The newest request overall, regardless of the selected range — the context
            # hint is about "how big is my context right now", and `requests` above is
            # range-scoped, so on a 1m/15m range it's empty whenever you've paused.
            newest = None
            for rec in self.msgs.values():
                if (proj is None or rec[4] == proj) and (newest is None or rec[1] > newest[1]):
                    newest = rec
            # How each visible model got its price (see _rates), so the cost card can say
            # so instead of quietly showing a figure that was adjusted. Keyed by the ids
            # the dashboard actually sees, so a dated id resolves too.
            models_in_scope = self.project_models.get(proj, self.models) if proj else self.models
            model_order = sorted(self.models, key=lambda m: self.model_first_ts.get(m, 0))
            pricing = {}
            for m in models_in_scope:
                note = rate_notes.get(_model_key(m))
                if note:
                    pricing[m] = {
                        k: round(v, 4) if isinstance(v, float) else v for k, v in note.items()
                    }

            # Keyed by the ids the dashboard actually sees, so a dated id resolves too.
            context_limits = {
                m: CONTEXT_LIMITS[_model_key(m)] for m in self.models if _model_key(m) in CONTEXT_LIMITS
            }
            latest_request = newest and {
                "t": round(newest[1], 1),
                "ctx": round(newest[2][2] + newest[2][3] + newest[2][4]),
                "model": newest[3],
            }

            def events_in(lo, hi):
                return sorted(
                    (
                        {"t": e[0], "kind": e[1], "extra": e[2]}
                        for e in self.events
                        if lo <= e[0] <= hi and (proj is None or e[3] == proj)
                    ),
                    key=lambda e: e["t"],
                )
            events = events_in(start, now + 1)
            activity = [
                {"kind": st[3], "since": st[4]}
                for st in self.files.values()
                if st[3] and now - st[4] < (MAX_SPREAD if st[3] == "gen" else TOOL_STALE)
                and (proj is None or st[6] == proj)
            ]
            payload = {
                "user": USER,
                "now": now,
                "start": start,
                "bucket": bucket,
                "range": range_key if range_key in RANGES else "1h",
                "series": {f: [round(max(v, 0.0), 2) for v in series[i]] for i, f in enumerate(FIELDS)},
                "today": {f: round(max(today[i], 0.0)) for i, f in enumerate(FIELDS)},
                "cum_base": {f: round(max(cum_base[i], 0.0), 2) for i, f in enumerate(FIELDS)},
                "cum_anchor": anchor,
                # Estimated equivalent pay-as-you-go API cost — not a real charge on a
                # flat subscription. See PRICING / Meter._rates for the rates and their source.
                "cost_series": [round(max(v, 0.0), 4) for v in cost_series],
                "cost_cum_base": round(max(cost_cum_base, 0.0), 4),
                "subscription_price": self.subscription_price,
                "pricing": pricing,
                "context_limits": context_limits,
                "cache15m": {"cw": round(max(cache15_cw, 0.0)), "cr": round(max(cache15_cr, 0.0))},
                # `models` is what the selected project used; `model_order` is every live
                # model, same first-used order. The dashboard takes a model's colour from its
                # place in `model_order`, so it keeps the same colour whichever project is
                # selected — an index into the scoped list moved it whenever the filter did.
                "models": [m for m in model_order if m in models_in_scope],
                "model_order": model_order,
                "series_model": {
                    m: {"out": [round(v, 2) for v in s["out"]], "in": [round(v, 2) for v in s["in"]]}
                    for m, s in series_model.items()
                },
                "requests": requests,
                "latest_request": latest_request,
                "events": events,
                "activity": activity,
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
            key = qs.get("range", ["1h"])[0]
            project = qs.get("project", [None])[0]
            body = json.dumps(self.meter.snapshot(key, project), separators=(",", ":")).encode()
            return self._send(200, body, "application/json")
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        url = urlparse(self.path)
        # A browser attaches Origin to every cross-site POST, and a "simple" one (e.g.
        # Content-Type: text/plain) is delivered without a preflight the server could
        # refuse — so without this check, any page you happen to have open could make
        # this endpoint spawn a WSL process. A missing Origin is a non-browser caller
        # (curl, a script), which was never the exposure.
        if self.headers.get("Origin") and not self._cors_origin():
            return self._send(403, b"cross-origin request refused", "text/plain")
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

    def _cors_origin(self):
        """The request's Origin if it's another meter's page on this machine, else None."""
        origin = self.headers.get("Origin")
        return origin if origin and LOCAL_ORIGIN_RE.match(origin) else None

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Lets a dashboard served by one meter (e.g. native Windows) fetch /api/data from
        # another meter on a different port (e.g. one running inside WSL) for the Local/
        # Remote source toggle, without either meter polling the other's filesystem.
        # Scoped to loopback origins rather than "*": this data includes every project
        # path you work in, and "*" handed it to any site you happened to have open.
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
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
    parser.add_argument(
        "--subscription-price", type=float, default=22.0,
        help="reference line ($) on the estimated-cost chart, e.g. your monthly Claude "
        "subscription price — for comparison only, not a real budget (default: 22)",
    )
    args = parser.parse_args()
    roots = args.projects or [default_projects_dir()]

    meter = Meter(roots, subscription_price=args.subscription_price)
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
