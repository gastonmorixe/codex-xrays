#!/usr/bin/env python3
import argparse
import curses
import errno
import io
import json
import os
import re
import signal
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional, Tuple, Set
from urllib.parse import urlparse


# Patterns and parsing helpers
SSE_JSON_RE = re.compile(r"SSE event:\s*(\{.*\})\s*$")
LEVEL_RE = re.compile(r"\b(TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\b")
ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z")
FUNCTIONCALL_JSON_RE = re.compile(r"FunctionCall:\s*(\{.*\})\s*$")


@dataclass
class ItemState:
    item_id: str
    type_label: str = ""
    output_index: Optional[int] = None
    last_seq: Optional[int] = None
    text: Deque[str] = field(default_factory=lambda: deque(maxlen=4096))
    # Keep a rolling window for memory safety
    char_budget: int = 8192
    updated_at: float = field(default_factory=time.time)

    def append_delta(self, delta: str, seq: Optional[int], tlabel: str, out_idx: Optional[int]):
        if not delta:
            return
        self.type_label = tlabel or self.type_label
        self.output_index = out_idx if out_idx is not None else self.output_index
        self.last_seq = seq if seq is not None else self.last_seq
        self.updated_at = time.time()
        # Append efficiently; track char budget
        self.text.append(delta)
        # Compact if beyond budget
        total = sum(len(x) for x in self.text)
        while total > self.char_budget and len(self.text) > 1:
            left = self.text.popleft()
            total -= len(left)

    def snapshot(self) -> str:
        return "".join(self.text)


class FileTail:
    def __init__(self, path: str, from_start: bool = False):
        self.path = path
        self.from_start = from_start
        self._fh: Optional[io.TextIOBase] = None
        self._ino: Optional[int] = None
        self._pos: int = 0

    def open(self):
        # Open in text mode with utf-8, ignore errors to be robust
        self._fh = open(self.path, "r", encoding="utf-8", errors="replace")
        try:
            st = os.fstat(self._fh.fileno())
            self._ino = st.st_ino
            if self.from_start:
                self._pos = 0
                self._fh.seek(0, os.SEEK_SET)
            else:
                self._pos = st.st_size
                self._fh.seek(0, os.SEEK_END)
        except Exception:
            self._ino = None

    def _reopen_if_rotated(self):
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return
        if self._ino is None or self._fh is None:
            return
        # Detect rotation or truncation
        if st.st_ino != self._ino or st.st_size < self._pos:
            try:
                self._fh.close()
            except Exception:
                pass
            self.open()

    def read_new_lines(self) -> Deque[str]:
        if self._fh is None:
            self.open()
        out: Deque[str] = deque()
        if self._fh is None:
            return out
        self._reopen_if_rotated()
        while True:
            line = self._fh.readline()
            if not line:
                break
            self._pos += len(line.encode("utf-8", errors="ignore"))
            out.append(line.rstrip("\n"))
        return out


def classify_event(etype: str) -> str:
    # Map to friendly labels
    if not etype:
        return "other"
    if etype.endswith(".delta"):
        return etype  # Keep full for detail
    return etype


def level_from_line(line: str) -> str:
    m = LEVEL_RE.search(line)
    return m.group(1) if m else "INFO"


def parse_sse_json(line: str) -> Optional[dict]:
    m = SSE_JSON_RE.search(line)
    if not m:
        return None
    payload = m.group(1)
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def shorten_id(item_id: str, keep: int = 10) -> str:
    if not item_id:
        return "<no-id>"
    if len(item_id) <= keep:
        return item_id
    return f"{item_id[:keep]}…"


class VizApp:
    def __init__(self, stdscr, file_path: str, from_start: bool = False, max_items: int = 200, lines_per_item: int = 5, pretty_preview: bool = False, pretty_mode: str = "summary", lines_expanded: int = 12):
        self.stdscr = stdscr
        self.tailer = FileTail(file_path, from_start=from_start)
        self.items: Dict[Tuple[str, int], ItemState] = {}
        self.max_items = max_items
        self.lines_per_item = lines_per_item
        self.pretty_preview = pretty_preview
        self.pretty_mode = pretty_mode
        self.lines_expanded = lines_expanded
        self.recent_other: Deque[str] = deque(maxlen=50)
        self.event_count = 0
        self.delta_count = 0
        self.start_time = time.time()
        self.last_fps_time = self.start_time
        self.last_event_count = 0
        self.eps = 0.0
        self.running = True
        self.paused = False
        # Selection and navigation
        self.selected_key: Optional[Tuple[str, int]] = None
        self.list_scroll = 0
        self.pinned: Set[Tuple[str, int]] = set()
        self.expanded: Set[Tuple[str, int]] = set()
        # Simple type filter: None means all
        self.type_filter: Optional[str] = None  # values: None, 'args', 'out', 'err'

    def setup_curses(self):
        curses.curs_set(0)
        self.stdscr.nodelay(True)
        self.stdscr.keypad(True)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            # Pair ids
            curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLUE)   # header
            curses.init_pair(2, curses.COLOR_CYAN, -1)                   # function args
            curses.init_pair(3, curses.COLOR_GREEN, -1)                  # output text
            curses.init_pair(4, curses.COLOR_MAGENTA, -1)                # tool/tool calls
            curses.init_pair(5, curses.COLOR_YELLOW, -1)                 # info
            curses.init_pair(6, curses.COLOR_RED, -1)                    # error
            curses.init_pair(7, curses.COLOR_WHITE, -1)                  # default
            curses.init_pair(8, curses.COLOR_BLACK, curses.COLOR_YELLOW) # warn badge
            curses.init_pair(9, curses.COLOR_BLACK, curses.COLOR_RED)    # error badge

    def color_for_type(self, tlabel: str) -> int:
        if not curses.has_colors():
            return curses.A_NORMAL
        if tlabel.endswith("function_call_arguments.delta"):
            return curses.color_pair(2)
        if tlabel.endswith("output_text.delta"):
            return curses.color_pair(3)
        if ".tool" in tlabel or ".function_call" in tlabel:
            return curses.color_pair(4)
        if "error" in tlabel.lower():
            return curses.color_pair(6)
        return curses.color_pair(7)

    def _ellipsize(self, s: str, limit: int) -> str:
        if limit <= 1:
            return "…" if s else ""
        s1 = " ".join(s.split())
        return s1 if len(s1) <= limit else (s1[: max(1, limit - 1)] + "…")

    def _tail_ellipsize(self, s: str, limit: int) -> str:
        if limit <= 1:
            return "…" if s else ""
        s1 = " ".join(s.split())
        return s1 if len(s1) <= limit else ("…" + s1[-(limit - 1):])

    def _jsonish_extract(self, text: str) -> dict:
        # Heuristic extraction from possibly partial JSON strings
        src = text[-2000:].strip()  # look at tail where args accumulate
        out: dict = {}
        # Common fields
        for key in ("name", "tool", "tool_name", "function", "action"):
            m = re.search(rf'"{key}"\s*:\s*"([^"]+)"', src)
            if m and not out.get("tool_name"):
                out["tool_name"] = m.group(1)
        for key in ("query", "q", "text", "prompt", "input"):
            m = re.search(rf'"{key}"\s*:\s*"(.+?)"', src)
            if m and not out.get("query"):
                out["query"] = m.group(1)
        for key in ("url", "uri"):
            m = re.search(rf'"{key}"\s*:\s*"(https?://[^"\s]+)"', src)
            if m and not out.get("url"):
                out["url"] = m.group(1)
        for key in ("file", "path", "filepath", "filename"):
            m = re.search(rf'"{key}"\s*:\s*"([^"\n]+)"', src)
            if m and not out.get("path"):
                out["path"] = m.group(1)
        for key in ("command", "cmd", "shell"):
            m = re.search(rf'"{key}"\s*:\s*"([^"\n]+)"', src)
            if m and not out.get("command"):
                out["command"] = m.group(1)
        # Command provided as an array of tokens
        m = re.search(r'"command"\s*:\s*\[(.*?)\]', src, re.S)
        if m and not out.get("command"):
            toks = re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', m.group(1))
            if toks:
                out["command"] = " ".join(t.replace('\\"', '"') for t in toks)
        # Extra metadata
        m = re.search(r'"with_escalated_permissions"\s*:\s*(true|false)', src, re.I)
        if m:
            out["with_escalated_permissions"] = (m.group(1).lower() == "true")
        m = re.search(r'"timeout_ms"\s*:\s*(\d+)', src)
        if m:
            out["timeout_ms"] = int(m.group(1))
        m = re.search(r'"justification"\s*:\s*"(.+?)"', src)
        if m:
            out["justification"] = m.group(1)
        # Fallback: try to load single JSON object safely
        if not out:
            try:
                obj = json.loads(src)
                if isinstance(obj, dict):
                    keys = list(obj.keys())
                    out["keys"] = keys[:6]
            except Exception:
                pass
        return out

    def _summarize_apply_patch(self, text: str, width: int) -> Optional[str]:
        if "apply_patch" not in text and "*** Begin Patch" not in text:
            return None
        m = re.search(r"\*\*\* Begin Patch(.*)\*\*\* End Patch", text, re.S)
        if not m:
            return None
        body = m.group(1)
        add = update = delete = 0
        plus = minus = 0
        files: list[tuple[str, str]] = []
        for ln in body.splitlines():
            if ln.startswith("*** Add File: "):
                add += 1
                path = ln.split(":", 1)[1].strip()
                files.append(("+", os.path.basename(path)))
                continue
            if ln.startswith("*** Update File: "):
                update += 1
                path = ln.split(":", 1)[1].strip()
                files.append(("✏️", os.path.basename(path)))
                continue
            if ln.startswith("*** Delete File: "):
                delete += 1
                path = ln.split(":", 1)[1].strip()
                files.append(("🗑️", os.path.basename(path)))
                continue
            if ln.startswith("+"):
                plus += 1
            elif ln.startswith("-"):
                minus += 1
        head = f"🧩 patch: {add}➕ {update}✏️ {delete}🗑️ · +{plus} −{minus}"
        if files:
            shown = " ".join(f"{mark} {name}" for mark, name in files[:3])
            extra = " …" if len(files) > 3 else ""
            head += " · " + self._ellipsize(shown + extra, max(12, width))
        return head

    def get_pretty_preview(self, st: ItemState, width: int) -> Tuple[str, int]:
        raw = st.snapshot()
        tlabel = (st.type_label or "").lower()
        s = raw.strip()
        parts: list[str] = []
        attr = self.color_for_type(tlabel)
        # Tool/function args → summarize fields
        if tlabel.endswith("function_call_arguments.delta") or s.startswith("{"):
            info = self._jsonish_extract(s)
            if info.get("tool_name"):
                parts.append(f"🧰 {info['tool_name']}")
            if info.get("query"):
                parts.append(f"🔎 {self._ellipsize(info['query'], max(8, width//2))}")
            if info.get("url"):
                try:
                    host = urlparse(info["url"]).netloc or info["url"]
                except Exception:
                    host = info["url"]
                parts.append(f"🔗 {self._ellipsize(host, max(8, width//3))}")
            if info.get("path"):
                parts.append(f"📄 {self._ellipsize(os.path.basename(info['path']), max(8, width//3))}")
            if info.get("command"):
                cmd_str = info["command"]
                patch_sum = self._summarize_apply_patch(cmd_str, width)
                if patch_sum:
                    parts.append(patch_sum)
                else:
                    parts.append(f"🛠️ {self._ellipsize(cmd_str, max(12, width//2))}")
            if info.get("with_escalated_permissions"):
                parts.append("🛡️ root")
            if info.get("timeout_ms"):
                parts.append(f"⏱️ {int(info['timeout_ms'])//1000}s")
            if info.get("justification"):
                parts.append(f"✍️ {self._ellipsize(info['justification'], max(10, width//2))}")
            if not parts and info.get("keys"):
                parts.append("🧰 args:" + ",".join(info["keys"]))
            if parts:
                # Color: cyan for args/tool summaries
                if curses.has_colors():
                    attr = curses.color_pair(2)
                summary = self._ellipsize("  ·  ".join(parts), width)
                if self.pretty_mode == "hybrid":
                    raw_tail = " ".join(raw.replace("\n", " ").split())
                    budget = max(width * max(1, self.lines_per_item - 1), width)
                    excerpt = self._tail_ellipsize(raw_tail, budget)
                    return summary + "\n" + excerpt, attr
                return summary, attr
        # Output text / explanations → decorate
        # Code fences
        if s.startswith("```"):
            if curses.has_colors():
                attr = curses.color_pair(5)
            summary = self._ellipsize("🧩 code block", width)
            if self.pretty_mode == "hybrid" and raw:
                excerpt = self._tail_ellipsize(s, max(width * max(1, self.lines_per_item - 1), width))
                return summary + "\n" + excerpt, attr
            return summary, attr
        # Obvious errors/warnings
        if re.search(r"\b(error|exception|traceback|failed)\b", s, re.I):
            if curses.has_colors():
                attr = curses.color_pair(6)
            summary = self._ellipsize("❌ " + s, width)
            return summary, attr
        if re.search(r"\b(warn|deprecate)\w*\b", s, re.I):
            if curses.has_colors():
                attr = curses.color_pair(5)
            summary = self._ellipsize("⚠️ " + s, width)
            return summary, attr
        # Links
        m = re.search(r"https?://[^\s]+", s)
        if m:
            try:
                host = urlparse(m.group(0)).netloc
            except Exception:
                host = m.group(0)
            if curses.has_colors():
                attr = curses.color_pair(5)
            summary = self._ellipsize(f"🔗 {host} — {s}", width)
            if self.pretty_mode == "hybrid" and raw:
                excerpt = self._tail_ellipsize(s, max(width * max(1, self.lines_per_item - 1), width))
                return summary + "\n" + excerpt, attr
            return summary, attr
        # Default speech bubble
        if tlabel.endswith("output_text.delta"):
            if curses.has_colors():
                attr = curses.color_pair(3)
            summary = self._ellipsize("💬 " + s, width)
            if self.pretty_mode == "hybrid" and s:
                excerpt = self._tail_ellipsize(s, max(width * max(1, self.lines_per_item - 1), width))
                return summary + "\n" + excerpt, attr
            return summary, attr
        return self._ellipsize(s, width), attr

    def render_recent_line(self, ln: str, width: int) -> Tuple[str, int]:
        # Try SSE JSON first
        obj = parse_sse_json(ln)
        if obj:
            t = (obj.get("type") or "").lower()
            attr = self.color_for_type(t)
            # Show item and out idx briefly
            meta = []
            if obj.get("item_id"):
                iid = shorten_id(str(obj["item_id"]))
                meta.append(f"{iid}")
            if isinstance(obj.get("output_index"), int):
                meta.append(f"#{obj['output_index']}")
            prefix = (" ".join(meta) + ": ") if meta else ""
            if t.endswith("function_call_arguments.delta"):
                info = self._jsonish_extract(obj.get("delta", ""))
                parts = []
                if info.get("tool_name"):
                    parts.append(f"🧰 {info['tool_name']}")
                if info.get("query"):
                    parts.append(f"🔎 {self._ellipsize(info['query'], max(8, width//2))}")
                if info.get("url"):
                    try:
                        host = urlparse(info['url']).netloc or info['url']
                    except Exception:
                        host = info['url']
                    parts.append(f"🔗 {self._ellipsize(host, max(8, width//3))}")
                if info.get("path"):
                    parts.append(f"📄 {self._ellipsize(os.path.basename(info['path']), max(8, width//3))}")
                if info.get("command"):
                    cmd_str = info["command"]
                    patch_sum = self._summarize_apply_patch(cmd_str, width)
                    if patch_sum:
                        parts.append(patch_sum)
                    else:
                        parts.append(f"🛠️ {self._ellipsize(cmd_str, max(8, width//3))}")
                if not parts and obj.get("delta"):
                    parts.append(self._ellipsize(obj["delta"], max(8, width)))
                s = prefix + ("  ·  ".join(parts) if parts else "🧰 args …")
                return self._ellipsize(s, width), attr
            if t.endswith("output_text.delta"):
                delta = (obj.get("delta") or "").strip()
                s = prefix + ("💬 " + delta if delta else "💬 …")
                return self._ellipsize(s, width), attr
            if "error" in t:
                msg = obj.get("message") or obj.get("error") or obj.get("delta") or "error"
                s = prefix + "❌ " + str(msg)
                return self._ellipsize(s, width), attr
            # default SSE
            s = prefix + f"📡 {obj.get('type','event')}"
            if obj.get("delta"):
                s += ": " + self._ellipsize(str(obj["delta"]), max(8, width//2))
            return self._ellipsize(s, width), attr
        # FunctionCall JSON lines
        m = FUNCTIONCALL_JSON_RE.search(ln)
        if m:
            try:
                fc = json.loads(m.group(1))
            except Exception:
                fc = None
            if isinstance(fc, dict):
                cmd = fc.get("command")
                if isinstance(cmd, list) and cmd:
                    cmd_str = " ".join(x for x in cmd if isinstance(x, str))
                else:
                    cmd_str = str(cmd)
                patch_sum = self._summarize_apply_patch(cmd_str or "", width)
                if patch_sum:
                    s = patch_sum
                else:
                    s = f"🛠️ call: {self._ellipsize(cmd_str, max(8, width))}"
                return self._ellipsize(s, width), self.color_for_type(".function_call")
        # Fallback: level badge + trimmed line
        lvl = level_from_line(ln)
        badge, attr = self.badge_for_level(lvl)
        ln_clean = ISO_TS_RE.sub("", ln).strip()
        s = f"{badge} {ln_clean}"
        return self._ellipsize(s, width), attr

    def badge_for_level(self, level: str) -> Tuple[str, int]:
        lvl = level.upper()
        if not curses.has_colors():
            return f"[{lvl}]", curses.A_BOLD
        if lvl in ("ERROR", "FATAL"):
            return f" {lvl} ", curses.color_pair(9) | curses.A_BOLD
        if lvl in ("WARN", "WARNING"):
            return f" {lvl} ", curses.color_pair(8) | curses.A_BOLD
        return f" {lvl} ", curses.color_pair(5) | curses.A_BOLD

    def handle_line(self, line: str):
        self.event_count += 1
        data = parse_sse_json(line)
        if data:
            etype = classify_event(data.get("type", ""))
            item_id = data.get("item_id") or data.get("id") or ""
            seq = data.get("sequence_number")
            out_idx = data.get("output_index")
            delta = data.get("delta") or ""
            if etype.endswith(".delta") and item_id:
                self.delta_count += 1
                key = (item_id, out_idx or 0)
                st = self.items.get(key)
                if not st:
                    st = ItemState(item_id=item_id)
                    self.items[key] = st
                    # Bound number of items
                    if len(self.items) > self.max_items:
                        # Drop the stalest item
                        oldest_key = min(self.items, key=lambda k: self.items[k].updated_at)
                        self.items.pop(oldest_key, None)
                st.append_delta(delta, seq, etype, out_idx)
                return
            # Non-delta SSE we still note
            self.recent_other.append(line)
            return

        # Not an SSE JSON line; keep a short tail
        self.recent_other.append(line)

    def draw(self):
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        # Header
        header = f" Codex Xrays 1.0 — {os.path.basename(self.tailer.path)} "
        filt = self.type_filter if self.type_filter else 'all'
        if not self.pretty_preview:
            pretty = 'off'
        else:
            pretty = self.pretty_mode
        stats = (
            f"events:{self.event_count} deltas:{self.delta_count} eps:{self.eps:0.1f} "
            f"items:{len(self.items)} filt:{filt} pretty:{pretty}"
        )
        head_str = header + (" ".join([""] * 2))
        head_line = (header + (" " * max(1, w - len(header) - len(stats) - 1)) + stats)[: max(0, w - 1)]
        if curses.has_colors():
            self.stdscr.addnstr(0, 0, head_line, w - 1, curses.color_pair(1) | curses.A_BOLD)
        else:
            self.stdscr.addnstr(0, 0, head_line, w - 1, curses.A_REVERSE)

        # Main area for items
        top = 1
        bottom = h - 5
        area_h = max(0, bottom - top + 1)

        # Assemble list: pinned first (most recently updated), then others
        def type_matches(lbl: str) -> bool:
            if not self.type_filter:
                return True
            if self.type_filter == 'args':
                return lbl.endswith('function_call_arguments.delta')
            if self.type_filter == 'out':
                return lbl.endswith('output_text.delta')
            if self.type_filter == 'err':
                return 'error' in lbl.lower()
            return True

        items_seq = list(self.items.items())
        items_seq.sort(key=lambda kv: kv[1].updated_at, reverse=True)
        pinned_items = [kv for kv in items_seq if kv[0] in self.pinned and type_matches(kv[1].type_label)]
        other_items = [kv for kv in items_seq if kv[0] not in self.pinned and type_matches(kv[1].type_label)]
        ordered = pinned_items + other_items

        # Maintain selection across frames
        keys_only = [k for k, _ in ordered]
        if self.selected_key not in keys_only and keys_only:
            self.selected_key = keys_only[0]

        # Ensure selection visibility by adjusting list_scroll
        selected_index = keys_only.index(self.selected_key) if self.selected_key in keys_only else 0

        # Precompute how many rows each item will consume (<= lines_per_item)
        def wrap_lines_for(st: ItemState, first_prefix_len: int, width: int, limit: int) -> list[str]:
            if self.pretty_preview:
                text, _attr = self.get_pretty_preview(st, width)
            else:
                text = st.snapshot()
            # Split by newlines then wrap hard at width
            lines: list[str] = []
            effective_width = max(1, width)
            # Replace carriage returns to avoid oddities
            for segment in text.replace('\r', '').split('\n'):
                s = segment
                while len(s) > effective_width:
                    lines.append(s[:effective_width])
                    s = s[effective_width:]
                lines.append(s)
            # Limit to N lines.
            if len(lines) > limit:
                if self.pretty_preview:
                    # Keep first summary line, then tail of remaining lines to preserve recency.
                    head = lines[:1]
                    tail_needed = max(0, limit - 1)
                    tail = lines[-tail_needed:] if tail_needed > 0 else []
                    lines = head + tail
                else:
                    # Normal mode: show last N lines
                    lines = lines[-limit:]
            return lines

        # Determine block heights and build view blocks
        blocks: list[Tuple[Tuple[str, int], list[str], str]] = []  # (key, lines, prefix)
        for (item_id, out_idx), st in ordered:
            prefix = f"{shorten_id(item_id, 12)}#{out_idx}: "
            avail = max(1, w - len(prefix) - 1)
            limit = self.lines_expanded if (item_id, out_idx) in self.expanded else self.lines_per_item
            lines = wrap_lines_for(st, len(prefix), avail, limit)
            blocks.append(((item_id, out_idx), lines, prefix))

        # Compute start row for each block based on scrolling
        # We scroll by full blocks so selection stays aligned
        total_blocks = len(blocks)
        if total_blocks > 0:
            # Adjust list_scroll to keep selection visible
            if selected_index < self.list_scroll:
                self.list_scroll = selected_index
            # Ensure selected block bottom within view
            while True:
                used = 0
                i = self.list_scroll
                fit_end = i
                while fit_end < total_blocks and used + max(1, len(blocks[fit_end][1])) <= area_h:
                    used += max(1, len(blocks[fit_end][1]))
                    fit_end += 1
                if selected_index < self.list_scroll:
                    self.list_scroll = max(0, selected_index)
                    continue
                if selected_index >= fit_end:
                    self.list_scroll = min(selected_index, max(0, total_blocks - 1))
                    continue
                break

        # Render blocks within viewport
        row = top
        i = self.list_scroll
        while i < total_blocks and row <= bottom:
            key, lines, prefix = blocks[i]
            st = self.items.get(key)
            if st is None:
                i += 1
                continue
            tlabel = st.type_label
            if self.pretty_preview:
                _preview_text, attr = self.get_pretty_preview(st, avail)
            else:
                attr = self.color_for_type(tlabel)
            sel = (key == self.selected_key)
            if sel:
                attr |= curses.A_REVERSE
            # First line has prefix
            avail = max(0, w - len(prefix) - 1)
            if row <= bottom:
                self.stdscr.addnstr(row, 0, prefix, len(prefix), curses.A_BOLD | (curses.A_REVERSE if sel else 0))
                if lines:
                    self.stdscr.addnstr(row, len(prefix), lines[0][:avail], avail, attr)
                row += 1
            # Remaining lines without prefix
            for ln in lines[1:]:
                if row > bottom:
                    break
                self.stdscr.addnstr(row, len(prefix), ln[:avail], avail, attr)
                row += 1
            i += 1

        # Secondary area for recent non-SSE lines
        help_y = h - 2
        recent_y = h - 6
        if recent_y > row:
            # Title
            title = " Recent logs "
            try:
                self.stdscr.addnstr(recent_y, 0, title, min(len(title), w - 1), curses.A_UNDERLINE)
            except curses.error:
                pass
            # Show last 3 recent lines compacted
            shown = list(self.recent_other)[-3:]
            ry = recent_y + 1
            for ln in shown:
                if ry >= help_y:
                    break
                if self.pretty_preview:
                    content, attr = self.render_recent_line(ln, max(0, w - 1))
                else:
                    lvl = level_from_line(ln)
                    badge, attr = self.badge_for_level(lvl)
                    ln_clean = ISO_TS_RE.sub("", ln).strip()
                    content = f"{badge} {ln_clean}"
                try:
                    self.stdscr.addnstr(ry, 0, content[: max(0, w - 1)], w - 1, attr)
                except curses.error:
                    pass
                ry += 1

        # Help/footer
        # Help/footer
        help_text = " q:quit  ↑/↓:select  ↩:open  x:pin  e:export  f:filter  c:clear  p:pause  s:toggle start  space:refresh  b:pretty-mode  m:more "
        try:
            self.stdscr.addnstr(help_y, 0, help_text[: max(0, w - 1)], w - 1, curses.A_DIM)
        except curses.error:
            pass

        self.stdscr.refresh()

    def loop(self):
        self.setup_curses()
        # Main loop
        while self.running:
            # Input
            try:
                ch = self.stdscr.getch()
            except curses.error:
                ch = -1
            if ch != -1:
                if ch in (ord('q'), ord('Q')):
                    self.running = False
                elif ch in (ord('c'), ord('C')):
                    self.items.clear()
                elif ch in (ord('p'), ord('P')):
                    self.paused = not self.paused
                elif ch in (ord('s'), ord('S')):
                    # Toggle start mode and reopen file
                    self.tailer.from_start = not self.tailer.from_start
                    try:
                        if self.tailer._fh:
                            self.tailer._fh.close()
                    except Exception:
                        pass
                    self.tailer._fh = None
                    self.tailer._ino = None
                    self.tailer._pos = 0
                elif ch in (curses.KEY_UP, ord('k')):
                    self._move_selection(-1)
                elif ch in (curses.KEY_DOWN, ord('j')):
                    self._move_selection(1)
                elif ch in (10, 13):
                    # Enter -> open detail view
                    if self.selected_key:
                        self.detail_view(self.selected_key)
                elif ch in (ord('x'), ord('X')):
                    if self.selected_key:
                        if self.selected_key in self.pinned:
                            self.pinned.remove(self.selected_key)
                        else:
                            self.pinned.add(self.selected_key)
                elif ch in (ord('e'), ord('E')):
                    if self.selected_key:
                        self.export_item(self.selected_key)
                elif ch in (ord('f'), ord('F')):
                    self._cycle_filter()
                elif ch == ord(' '):
                    pass  # manual refresh
                elif ch in (ord('b'), ord('B')):
                    # Cycle pretty mode: off -> summary -> hybrid -> off
                    if not self.pretty_preview:
                        self.pretty_preview = True
                        self.pretty_mode = "summary"
                    elif self.pretty_mode == "summary":
                        self.pretty_mode = "hybrid"
                    else:
                        self.pretty_preview = False
                        self.pretty_mode = "summary"
                elif ch in (ord('m'), ord('M')):
                    # Toggle expanded lines for the selected item
                    if self.selected_key:
                        if self.selected_key in self.expanded:
                            self.expanded.remove(self.selected_key)
                        else:
                            self.expanded.add(self.selected_key)

            # Read lines
            if not self.paused:
                lines = self.tailer.read_new_lines()
                for ln in lines:
                    self.handle_line(ln)

            # Update EPS every 0.5s
            now = time.time()
            if now - self.last_fps_time >= 0.5:
                delta_e = self.event_count - self.last_event_count
                self.eps = delta_e / (now - self.last_fps_time + 1e-9)
                self.last_event_count = self.event_count
                self.last_fps_time = now

            # Draw
            self.draw()
            time.sleep(0.02)  # ~50 FPS loop cap

    def _cycle_filter(self):
        # None -> args -> out -> err -> None
        order = [None, 'args', 'out', 'err']
        try:
            idx = order.index(self.type_filter)
        except ValueError:
            idx = 0
        self.type_filter = order[(idx + 1) % len(order)]

    def _move_selection(self, delta_blocks: int):
        if not self.items:
            return
        def type_matches(lbl: str) -> bool:
            if not self.type_filter:
                return True
            if self.type_filter == 'args':
                return lbl.endswith('function_call_arguments.delta')
            if self.type_filter == 'out':
                return lbl.endswith('output_text.delta')
            if self.type_filter == 'err':
                return 'error' in lbl.lower()
            return True
        items_seq = list(self.items.items())
        items_seq.sort(key=lambda kv: kv[1].updated_at, reverse=True)
        pinned_items = [kv for kv in items_seq if kv[0] in self.pinned and type_matches(kv[1].type_label)]
        other_items = [kv for kv in items_seq if kv[0] not in self.pinned and type_matches(kv[1].type_label)]
        keys = [k for k, _ in (pinned_items + other_items)]
        if self.selected_key not in keys:
            self.selected_key = keys[0]
            return
        idx = keys.index(self.selected_key)
        idx = max(0, min(len(keys) - 1, idx + delta_blocks))
        self.selected_key = keys[idx]

    def export_item(self, key: Tuple[str, int]):
        st = self.items.get(key)
        if not st:
            return
        item_id, out_idx = key
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", item_id)
        ts = time.strftime('%Y%m%d_%H%M%S')
        path = f"streamviz_export_{safe_id}_{out_idx}_{ts}.txt"
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(st.snapshot())
            # flash a small message in recent logs
            self.recent_other.append(f"INFO export -> {path}")
        except Exception as e:
            self.recent_other.append(f"ERROR export failed: {e}")

    def detail_view(self, key: Tuple[str, int]):
        # Fullscreen viewer with scrolling and live updates
        scroll = 0
        while True:
            self.stdscr.erase()
            h, w = self.stdscr.getmaxyx()
            st = self.items.get(key)
            if not st:
                msg = "<item no longer available>"
                self.stdscr.addnstr(0, 0, msg, min(len(msg), w - 1), curses.A_REVERSE)
                self.stdscr.refresh()
                time.sleep(0.5)
                return
            item_id, out_idx = key
            title = f" View — {item_id}#{out_idx} [{st.type_label}] "
            if curses.has_colors():
                self.stdscr.addnstr(0, 0, title[: w - 1], w - 1, curses.color_pair(1) | curses.A_BOLD)
            else:
                self.stdscr.addnstr(0, 0, title[: w - 1], w - 1, curses.A_REVERSE)
            # Prepare lines with wrapping
            content = st.snapshot().replace('\r', '')
            lines: list[str] = []
            for seg in content.split('\n'):
                s = seg
                while len(s) > w - 2:
                    lines.append(s[: w - 2])
                    s = s[w - 2:]
                lines.append(s)
            view_h = h - 2
            max_scroll = max(0, len(lines) - view_h)
            scroll = max(0, min(max_scroll, scroll))
            # Draw window
            row = 1
            end = min(len(lines), scroll + view_h)
            for i in range(scroll, end):
                self.stdscr.addnstr(row, 0, lines[i][: w - 1], w - 1, self.color_for_type(st.type_label))
                row += 1
            # Footer
            footer = " ↑/↓/PgUp/PgDn/Home/End scroll  e:export  x:pin  q/ESC:back "
            self.stdscr.addnstr(h - 1, 0, footer[: w - 1], w - 1, curses.A_DIM)
            self.stdscr.refresh()

            # Non-blocking input
            try:
                ch = self.stdscr.getch()
            except curses.error:
                ch = -1
            if ch == -1:
                # keep tailing in background
                if not self.paused:
                    lines_new = self.tailer.read_new_lines()
                    for ln in lines_new:
                        self.handle_line(ln)
                time.sleep(0.02)
                continue
            if ch in (ord('q'), 27):  # q or ESC
                return
            if ch in (curses.KEY_UP, ord('k')):
                scroll -= 1
            elif ch in (curses.KEY_DOWN, ord('j')):
                scroll += 1
            elif ch == curses.KEY_PPAGE:  # PgUp
                scroll -= (h - 4)
            elif ch == curses.KEY_NPAGE:  # PgDn
                scroll += (h - 4)
            elif ch in (curses.KEY_HOME, ord('g')):
                scroll = 0
            elif ch in (curses.KEY_END, ord('G')):
                scroll = 10**9
            elif ch in (ord('e'), ord('E')):
                self.export_item(key)
            elif ch in (ord('x'), ord('X')):
                if key in self.pinned:
                    self.pinned.remove(key)
                else:
                    self.pinned.add(key)


def main():
    parser = argparse.ArgumentParser(description="Real-time streaming log visualizer for Codex TUI logs.")
    parser.add_argument("--file", "-f", default=os.path.join(os.getcwd(), "codex-tui.log"), help="Path to log file to follow")
    parser.add_argument("--from-start", action="store_true", help="Read from start instead of tailing from end")
    parser.add_argument("--max-items", type=int, default=200, help="Max distinct item_id streams to track")
    parser.add_argument("--lines-per-item", "-L", type=int, default=5, help="Maximum wrapped lines to show per entry in list view")
    parser.add_argument("--lines-expanded", type=int, default=12, help="Lines to show when an item is expanded with 'm'")
    parser.add_argument("--pretty-preview", action="store_true", help="Render emoji/parsed previews in list view (or set XRAYS_PRETTY=1)")
    parser.add_argument("--pretty-mode", choices=["summary", "hybrid"], help="Pretty preview style when enabled")
    args = parser.parse_args()

    if not os.path.exists(args.file):
        print(f"Log file not found: {args.file}", file=sys.stderr)
        return 2

    # Handle Ctrl+C cleanly
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    # Env override for pretty previews
    if not args.pretty_preview:
        env_flag = os.environ.get("XRAYS_PRETTY") or os.environ.get("XRAYS_PRETTY_PREVIEW")
        if isinstance(env_flag, str) and env_flag.strip().lower() in {"1", "true", "yes", "on"}:
            args.pretty_preview = True
    # Pretty mode from env/arg
    if not args.pretty_mode:
        env_mode = (os.environ.get("XRAYS_PRETTY_MODE") or "").strip().lower()
        if env_mode in {"summary", "hybrid"}:
            args.pretty_mode = env_mode
        elif args.pretty_preview:
            args.pretty_mode = "hybrid"
        else:
            args.pretty_mode = "summary"

    def wrapped(stdscr):
        app = VizApp(
            stdscr,
            file_path=args.file,
            from_start=args.from_start,
            max_items=args.max_items,
            lines_per_item=args.lines_per_item,
            pretty_preview=args.pretty_preview,
            pretty_mode=args.pretty_mode,
            lines_expanded=args.lines_expanded,
        )
        app.loop()

    curses.wrapper(wrapped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
