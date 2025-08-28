#!/usr/bin/env python3
import argparse
import curses
import io
import json
import os
import re
import signal
import sys
import time
import textwrap
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional, Tuple, Set
from urllib.parse import urlparse


# Version
__version__ = "1.0.1"

# Patterns and parsing helpers
SSE_JSON_RE = re.compile(r"SSE event:\s*(\{.*\})\s*$")
LEVEL_RE = re.compile(r"\b(TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\b")
ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z")
ANSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
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
    def __init__(self, stdscr, file_path: str, from_start: bool = False, max_items: int = 200, lines_per_item: int = 5, pretty_preview: bool = False, pretty_mode: str = "summary", lines_expanded: int = 12, strip_ansi: bool = True, json_pretty: bool = False):
        self.stdscr = stdscr
        self.tailer = FileTail(file_path, from_start=from_start)
        self.items: Dict[Tuple[str, int], ItemState] = {}
        self.max_items = max_items
        self.lines_per_item = lines_per_item
        self.pretty_preview = pretty_preview
        self.pretty_mode = pretty_mode
        self.lines_expanded = lines_expanded
        self.strip_ansi = strip_ansi
        self.json_pretty = json_pretty
        self.json_wrap: bool = True
        self.recent_other: Deque[str] = deque(maxlen=50)
        # Follow mode: when True, keep viewport at newest (top). When False, show banner if new items arrive.
        self.follow_top: bool = True
        self.new_since: int = 0
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
        try:
            curses.set_escdelay(25)
        except Exception:
            pass
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

    def _has_colors_safe(self) -> bool:
        try:
            return curses.has_colors()
        except curses.error:
            return False

    def color_for_type(self, tlabel: str) -> int:
        if not self._has_colors_safe():
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

    def _strip_ansi(self, s: str) -> str:
        return ANSI_RE.sub("", s)

    def _wrap_text(self, text: str, width: int) -> list[str]:
        lines: list[str] = []
        if width <= 0:
            return [text]
        for segment in text.replace('\r', '').split('\n'):
            s = segment
            while len(s) > width:
                lines.append(s[:width])
                s = s[width:]
            lines.append(s)
        return lines

    def _pretty_json_lines(self, content: str) -> Optional[list[str]]:
        s = content.strip()
        if not s or (not s.startswith('{') and not s.startswith('[')):
            return None
        try:
            obj = json.loads(s)
        except Exception:
            return None
        try:
            pretty = json.dumps(obj, indent=2, ensure_ascii=False)
        except Exception:
            return None
        return pretty.split('\n')

    def _draw_json_line(self, row: int, col: int, text: str, width: int, default_attr: int):
        # Simple highlighter: indent + key (cyan) + punctuation default; values by type
        if not self._has_colors_safe():
            try:
                self.stdscr.addnstr(row, col, text[: width], width, default_attr)
            except curses.error:
                pass
            return
        key_m = re.match(r'^(\s*)"([^"\\]*(?:\\.[^"\\]*)*)"\s*:(\s*)(.*)$', text)
        if not key_m:
            # color booleans/null/numbers/strings lightly
            val = text.strip()
            attr = default_attr
            if val.startswith('"'):
                attr = curses.color_pair(3)
            elif re.match(r'^-?\d', val):
                attr = curses.color_pair(5)
            elif val.startswith(('true','false','null')):
                attr = curses.color_pair(4)
            try:
                self.stdscr.addnstr(row, col, text[: width], width, attr)
            except curses.error:
                pass
            return
        indent, key, gap, rest = key_m.groups()
        x = col
        try:
            self.stdscr.addnstr(row, x, indent[: width - (x-col)], width - (x-col), default_attr)
        except curses.error:
            pass
        x += len(indent)
        ktxt = '"' + key + '"'
        try:
            self.stdscr.addnstr(row, x, ktxt[: max(0, width - (x-col))], max(0, width - (x-col)), curses.color_pair(2))
        except curses.error:
            pass
        x += len(ktxt)
        try:
            self.stdscr.addnstr(row, x, ':' + gap, min(len(':'+gap), max(0, width - (x-col))), default_attr)
        except curses.error:
            pass
        x += len(':'+gap)
        # value coloring
        val_attr = default_attr
        rv = rest.lstrip()
        if rv.startswith('"'):
            val_attr = curses.color_pair(3)
        elif re.match(r'^-?\d', rv):
            val_attr = curses.color_pair(5)
        elif rv.startswith(('true','false','null')):
            val_attr = curses.color_pair(4)
        try:
            self.stdscr.addnstr(row, x, rest[: max(0, width - (x-col))], max(0, width - (x-col)), val_attr)
        except curses.error:
            pass

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
        cand = text
        # If we see escaped newlines but no real newlines, unescape a lightweight view
        if "\\n" in cand and "\n" not in cand:
            cand = cand.replace("\\r", "").replace("\\n", "\n").replace('\\"', '"')
        i0 = cand.find("*** Begin Patch")
        if i0 == -1:
            return None
        i1 = cand.find("*** End Patch", i0)
        partial = False
        if i1 == -1:
            partial = True
            i1 = len(cand)
        body = cand[i0 + len("*** Begin Patch"): i1]
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
        if partial:
            head += " (partial)"
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
                if self._has_colors_safe():
                    attr = curses.color_pair(2)
                summary = self._ellipsize("  ·  ".join(parts), width)
                return summary, attr
        # Output text / explanations → decorate
        # Code fences
        if s.startswith("```"):
            if self._has_colors_safe():
                attr = curses.color_pair(5)
            summary = self._ellipsize("🧩 code block", width)
            return summary, attr
        # Obvious errors/warnings
        if re.search(r"\b(error|exception|traceback|failed)\b", s, re.I):
            if curses.has_colors():
                attr = curses.color_pair(6)
            summary = self._ellipsize("❌ " + s, width)
            return summary, attr
        if re.search(r"\b(warn|deprecate)\w*\b", s, re.I):
            if self._has_colors_safe():
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
            return summary, attr
        # Default speech bubble
        if tlabel.endswith("output_text.delta"):
            # Summarize patch envelopes even when streaming in text deltas
            if "*** Begin Patch" in s or "apply_patch" in s:
                p = self._summarize_apply_patch(s, width)
                if p:
                    return self._ellipsize(p, width), attr
            if self._has_colors_safe():
                attr = curses.color_pair(3)
            summary = self._ellipsize("💬 " + s, width)
            return summary, attr
        return self._ellipsize(s, width), attr

    def render_recent_line(self, ln: str, width: int) -> Tuple[str, int]:
        # Sanitize ANSI if requested
        try:
            if self.strip_ansi:
                ln = self._strip_ansi(ln)
        except Exception:
            pass
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
                if "*** Begin Patch" in delta or "apply_patch" in delta:
                    ps = self._summarize_apply_patch(delta, width)
                    if ps:
                        return self._ellipsize((prefix + ps).strip(), width), attr
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

    def preview_lines_for_pretty(self, st: ItemState, width: int, limit: int) -> list[str]:
        summary, _a = self.get_pretty_preview(st, width)
        summary_lines = self._wrap_text(summary, max(1, width))
        if self.pretty_mode == 'summary' or limit <= len(summary_lines):
            return summary_lines[: max(1, limit)]
        remain = max(0, limit - len(summary_lines))
        raw = st.snapshot().replace('\r', '').replace('\n', ' ')
        shown_chars = sum(len(s) for s in summary_lines)
        raw_after = raw[shown_chars:] if shown_chars < len(raw) else ''
        tail_chars = width * remain
        tail = self._tail_ellipsize(raw_after, tail_chars)
        tail_lines = self._wrap_text(tail, max(1, width))
        if len(tail_lines) > remain:
            tail_lines = tail_lines[-remain:]
        return summary_lines + tail_lines

    def badge_for_level(self, level: str) -> Tuple[str, int]:
        lvl = level.upper()
        if not self._has_colors_safe():
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
                    if not self.follow_top:
                        self.new_since += 1
                    # Bound number of items
                    if len(self.items) > self.max_items:
                        # Drop the stalest item
                        oldest_key = min(self.items, key=lambda k: self.items[k].updated_at)
                        self.items.pop(oldest_key, None)
                st.append_delta(delta, seq, etype, out_idx)
                return
            # Non-delta SSE we still note
            ln = self._strip_ansi(line) if hasattr(self, 'strip_ansi') and self.strip_ansi else line
            self.recent_other.append(ln)
            return

        # Not an SSE JSON line; keep a short tail
        ln = self._strip_ansi(line) if hasattr(self, 'strip_ansi') and self.strip_ansi else line
        self.recent_other.append(ln)

    def draw(self):
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        # Header (no path). Show FOLLOWING badge when follow mode is on.
        header = f" 🛟 CodeXRays {__version__}"
        if self.follow_top:
            header += " [FOLLOWING]"
        header += " "
        filt = self.type_filter if self.type_filter else 'all'
        if not self.pretty_preview:
            pretty = 'off'
        else:
            pretty = self.pretty_mode
        stats = (
            f"events:{self.event_count} deltas:{self.delta_count} eps:{self.eps:0.1f} "
            f"items:{len(self.items)} filt:{filt} pretty:{pretty}"
        )
        head_line = (header + (" " * max(1, w - len(header) - len(stats) - 1)) + stats)[: max(0, w - 1)]
        if curses.has_colors():
            # Always render header in blue; following indicated by badge
            self.stdscr.addnstr(0, 0, head_line, w - 1, curses.color_pair(1) | curses.A_BOLD)
        else:
            self.stdscr.addnstr(0, 0, head_line, w - 1, curses.A_REVERSE)

        # Follow banner if not following and we have unseen updates
        banner: Optional[str] = None
        if not self.follow_top and self.new_since > 0:
            banner = f" ({self.new_since}) newer logs -> press T to follow"
        # Main area for items
        top = 2 if banner else 1
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
        if self.follow_top and keys_only:
            # Always select newest and keep viewport at top
            self.selected_key = keys_only[0]
            self.list_scroll = 0
            self.new_since = 0
        elif self.selected_key not in keys_only and keys_only:
            self.selected_key = keys_only[0]

        # Ensure selection visibility by adjusting list_scroll
        selected_index = keys_only.index(self.selected_key) if self.selected_key in keys_only else 0

        # Precompute how many rows each item will consume (<= lines_per_item)
        def wrap_lines_for(st: ItemState, first_prefix_len: int, width: int, limit: int) -> list[str]:
            if self.pretty_preview:
                # Build explicit summary + live tail lines so the newest text is visible.
                summary, _a = self.get_pretty_preview(st, width)
                summary_lines = self._wrap_text(summary, max(1, width))
                if self.pretty_mode == 'summary' or limit <= len(summary_lines):
                    return summary_lines[: max(1, limit)]
                remain = max(0, limit - len(summary_lines))
                raw = st.snapshot().replace('\r', '').replace('\n', ' ')
                # Avoid duplicating what was already shown in summary by skipping that many visible chars
                shown_chars = sum(len(s) for s in summary_lines)
                raw_after = raw[shown_chars:] if shown_chars < len(raw) else ''
                tail_chars = width * remain
                tail = self._tail_ellipsize(raw_after, tail_chars)
                tail_lines = self._wrap_text(tail, max(1, width))
                if len(tail_lines) > remain:
                    tail_lines = tail_lines[-remain:]
                return summary_lines + tail_lines
            # Plain mode: wrap and show last N lines
            lines = self._wrap_text(st.snapshot(), max(1, width))
            if len(lines) > limit:
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
                _summary, attr = self.get_pretty_preview(st, avail)
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

        # Follow banner row
        if banner:
            try:
                self.stdscr.addnstr(1, 0, banner[: max(0, w - 1)], w - 1, curses.A_BOLD)
            except curses.error:
                pass

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
        help_text = " q:quit  ↑/↓:select  ↩:open  x:pin  e:export  f:filter  c:clear  p:pause  s:toggle start  T:follow  space:refresh  b:pretty-mode  m:more "
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
                    if self.follow_top:
                        # leaving follow mode
                        self.follow_top = False
                        self.new_since = 0
                    self._move_selection(-1)
                elif ch in (curses.KEY_DOWN, ord('j')):
                    if self.follow_top:
                        self.follow_top = False
                        self.new_since = 0
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
                elif ch in (ord('T'),):
                    # Follow newest: jump to top and reset counter
                    self.follow_top = True
                    self.new_since = 0
                    # Move selection to newest item
                    items_seq = list(self.items.items())
                    items_seq.sort(key=lambda kv: kv[1].updated_at, reverse=True)
                    if items_seq:
                        self.selected_key = items_seq[0][0]
                    self.list_scroll = 0

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
        # User-initiated selection movement implies leaving follow mode
        self.follow_top = False
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
        path = f"codexrays_export_{safe_id}_{out_idx}_{ts}.txt"
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
            # Prepare lines with wrapping (optionally pretty JSON)
            content = st.snapshot().replace('\r', '')
            json_lines: Optional[list[str]] = None
            if self.json_pretty:
                json_lines = self._pretty_json_lines(content)
            if json_lines is None:
                lines: list[str] = []
                for seg in content.split('\n'):
                    s = seg
                    while len(s) > w - 2:
                        lines.append(s[: w - 2])
                        s = s[w - 2:]
                    lines.append(s)
                draw_colored = False
            else:
                if self.json_wrap:
                    # Word-wrap each pretty JSON line preserving indent
                    wrapped: list[str] = []
                    avail = max(1, w - 2)
                    for ln in json_lines:
                        indent = len(ln) - len(ln.lstrip())
                        wrapped.extend(textwrap.wrap(
                            ln,
                            width=avail,
                            break_long_words=False,
                            break_on_hyphens=False,
                            subsequent_indent=' ' * indent,
                            replace_whitespace=False,
                            drop_whitespace=False,
                        ) or [ln])
                    lines = wrapped
                else:
                    lines = json_lines
                draw_colored = True
            view_h = h - 2
            max_scroll = max(0, len(lines) - view_h)
            scroll = max(0, min(max_scroll, scroll))
            # Draw window
            row = 1
            end = min(len(lines), scroll + view_h)
            for i in range(scroll, end):
                if draw_colored:
                    self._draw_json_line(row, 0, lines[i], w - 1, self.color_for_type(st.type_label))
                else:
                    self.stdscr.addnstr(row, 0, lines[i][: w - 1], w - 1, self.color_for_type(st.type_label))
                row += 1
            # Footer
            wrap_state = 'on' if (self.json_pretty and self.json_wrap) else 'off'
            footer = f" ↑/↓/PgUp/PgDn/Home/End scroll  w:wrap({wrap_state})  e:export  x:pin  q/ESC:back "
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
            elif ch in (ord('w'), ord('W')):
                # Toggle JSON wrap in detail view
                self.json_wrap = not self.json_wrap
            elif ch in (ord('e'), ord('E')):
                self.export_item(key)
            elif ch in (ord('x'), ord('X')):
                if key in self.pinned:
                    self.pinned.remove(key)
                else:
                    self.pinned.add(key)


def main():
    parser = argparse.ArgumentParser(description="Real-time streaming log visualizer for Codex TUI logs.")
    default_log = os.path.expanduser("~/.codex/log/codex-tui.log")
    parser.add_argument("--file", "-f", default=default_log, help=f"Path to log file to follow (default: {default_log})")
    parser.add_argument("--from-start", action="store_true", help="Read from start instead of tailing from end")
    parser.add_argument("--max-items", type=int, default=200, help="Max distinct item_id streams to track")
    parser.add_argument("--lines-per-item", "-L", type=int, default=5, help="Maximum wrapped lines to show per entry in list view")
    parser.add_argument("--lines-expanded", type=int, default=12, help="Lines to show when an item is expanded with 'm'")
    parser.add_argument("--pretty-preview", action="store_true", help="Render emoji/parsed previews in list view (or set XRAYS_PRETTY=1)")
    parser.add_argument("--pretty-mode", choices=["summary", "hybrid"], help="Pretty preview style when enabled")
    parser.add_argument("--json-pretty", action="store_true", help="In detail view, pretty-print JSON with simple colors")
    parser.add_argument("--keep-ansi", action="store_true", help="Do not strip ANSI color codes from recent logs")
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
    # ANSI stripping via env
    if not args.keep_ansi:
        env_keep = (os.environ.get("XRAYS_KEEP_ANSI") or "").strip().lower()
        if env_keep in {"1", "true", "yes", "on"}:
            args.keep_ansi = True

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
            strip_ansi=(not args.keep_ansi),
            json_pretty=args.json_pretty,
        )
        app.loop()

    curses.wrapper(wrapped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
