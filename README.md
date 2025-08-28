# Codex Xrays 1.0

A fast, colorful, terminal UI that visualizes streaming Codex logs in real time. It tails `codex-tui.log`, parses SSE events, aggregates fast deltas by `item_id`, and renders a live dashboard with multiline entries, selection, pinning, filters, and a full‑screen detail view.

> Built to handle very rapid, tiny delta events and show them as coherent streams like:
>
> `fc_68af97f4d…#1: that require` → continuously appends as deltas arrive.


## Highlights

- Real‑time tailing of `codex-tui.log` with log‑rotation detection.
- Delta aggregation per `(item_id, output_index)`; renders colorized, multiline entries.
- Selection with arrow keys, full‑screen detail view, and live updates.
- Pin items to the top, filter by event type, and export selected content to a file.
- Shows recent non‑SSE lines for additional context (INFO/WARN/ERROR badges).
- Memory‑safe rolling buffers and high‑frequency rendering loop tuned for fast streams.


## Quick Start

- Requirements: Python 3.8+.
  - macOS/Linux: comes with `curses`.
  - Windows: `pip install windows-curses`.

- From the log directory:

```bash
python3 streamviz.py                                  # tail codex-tui.log from the end
python3 streamviz.py --from-start                     # read from beginning
python3 streamviz.py -f ./codex-tui.log -L 8 --max-items 300
python3 streamviz.py --pretty-preview                 # emoji + parsed previews
python3 streamviz.py --pretty-preview --pretty-mode hybrid   # summary + raw excerpt
# or via env var (auto-on):
XRAYS_PRETTY=1 python3 streamviz.py
XRAYS_PRETTY=1 XRAYS_PRETTY_MODE=summary python3 streamviz.py
```


## Keybindings

- q: quit
- ↑/↓ or j/k: move selection
- Enter: open full‑screen detail view of the selected entry
- x: pin/unpin selected entry (pinned items stay on top)
- f: cycle filter (all → args.delta → output.delta → error → all)
- e: export selected entry content to a `streamviz_export_*.txt` file
- b: cycle pretty mode (off → summary → hybrid)
- m: toggle more lines for selected item (uses `--lines-expanded`)
- p: pause/resume tailing
- s: toggle "from start" mode and reopen the file
- Space: manual refresh


## Command‑line Options

- `-f, --file <path>`: Path to the log file. Default: `./codex-tui.log`.
- `--from-start`: Read from the start of the file (otherwise tail from end).
- `--max-items <N>`: Max distinct `(item_id, output_index)` streams to keep in memory. Default: 200.
- `-L, --lines-per-item <N>`: Maximum wrapped lines to render per entry in list view. Default: 5.
- `--lines-expanded <N>`: Lines to show when an item is expanded with `m`. Default: 12.
- `--pretty-preview`: Enable emoji + parsed previews (can also set `XRAYS_PRETTY=1`).
- `--pretty-mode <summary|hybrid>`: Summary only, or summary plus a raw excerpt beneath.


## What the UI Shows

- Header: filename, counters (events, deltas), EPS (events/second), total items, active filter, and `pretty:off|summary|hybrid` indicator.
- Main list: most recently updated entries first. Each entry shows `short_item_id#output_index:` plus up to `N` wrapped lines of the latest content. Colors map to event types.
- Recent logs: compact tail of non‑delta events. With pretty mode, SSE/FunctionCall lines are summarized (🧰 tools, 🔎 queries, 🔗 hosts, 📄 files, 🛠️ commands, 💬 text).

## Configuration
- `XRAYS_PRETTY=1`: Start with pretty previews enabled (same as `--pretty-preview`).
- `XRAYS_PRETTY_MODE=summary|hybrid`: Choose preview style when enabled.


## Parsing Logic

- Lines matching `... SSE event: {json}` are parsed as JSON.
- If `type` ends with `.delta` and has `item_id`, the `delta` string is appended to that stream, keyed by `(item_id, output_index)`.
- Non‑delta SSE lines and all other non‑SSE log lines are kept in a small “Recent logs” buffer.
- Event type coloring:
  - `response.function_call_arguments.delta`: cyan
  - `response.output_text.delta`: green
  - Tool/function call related types: magenta
  - Types containing `error`: red
  - Others: default


## Multiline Rendering

- Each entry renders up to `--lines-per-item` wrapped lines.
- In normal mode, shows the most recent lines; in pretty mode, shows the summary first (and, in `hybrid`, a raw excerpt beneath).
- Wrapping is character‑based to ensure speed and deterministic layout.
- The selection auto‑scrolls to remain visible as new events push items.


## Full‑screen Detail View

- Press Enter on a selected entry to open a full‑screen viewer.
- Scroll with ↑/↓/PgUp/PgDn/Home/End. Live updates continue while viewing.
- Use `e` to export the full content; `x` to pin/unpin; `q` or ESC to return.


## Export

- `e` writes the selected item’s content to `streamviz_export_<id>_<idx>_<timestamp>.txt` in the current directory.


## Performance Notes

- File tailing uses non‑blocking reads with reopen on rotation/truncation.
- Each stream keeps a rolling window (character budget) to bound memory.
- The UI loop targets ~50 FPS; EPS is sampled every 0.5 seconds.


## Troubleshooting

- No colors: ensure your terminal supports ANSI colors; `TERM` should be something like `xterm-256color`.
- Windows: install `windows-curses` (`pip install windows-curses`). Run from a Unicode‑capable terminal.
- Large files: prefer tail mode (default). Use `--from-start` only when needed.
- Log path: pass `-f /path/to/codex-tui.log` if not running in the log folder.
- Flicker: prefer a modern terminal and a reasonable window size; the app uses full‑screen redraws for speed.


## Currently Implemented

- Real‑time tail with rotation detection
- Delta aggregation per `(item_id, output_index)`
- Multiline wrapped rendering with per‑entry cap (`-L`)
- Selection + full‑screen detail view with live updates
- Pin/unpin entries
- Type filters (all/args/output/error)
- Export selected content
- Recent non‑SSE logs panel with level badges
- Stats header: events, deltas, EPS, items, filter


## Roadmap / Ideas

- Search: `/` to search within the full‑screen view and list; `n/N` to jump.
- Copy to clipboard: `y` yank current selection (platform‑aware backends).
- Rich JSON rendering: pretty‑print structured payloads with syntax highlighting.
- Sequence integrity: visualize gaps or out‑of‑order `sequence_number` with badges.
- Grouping and tabs: group entries by session/run; tabbed views per group.
- Saved views & filters: persist pins and filters across runs.
- Alerting: simple rules to badge or beep on matches (e.g., errors, keywords).
- Theming: light/dark themes and custom color pairs.
- Input sources: follow multiple files, named pipes, or sockets; merge streams.
- Export bundles: save a session (pins + selected entries + metadata) to a folder.
- Web/remote view: optional headless server + web UI mirroring the TUI.

### UX & Interaction
- Per‑item pretty toggle: expand only the selected item’s preview (`B`).
- Detail view prettify: optional parsed rendering in the fullscreen view (`D`).
- Multi‑file run: `--files a.log b.log` with per‑file badges and filters.

### Preview & Parsing
- Tool taxonomy: map common tools to friendly names and surface relevant knobs (e.g., `top_k`, `temperature`).
- Redaction: detect and mask secrets (API keys, Bearer tokens, high‑entropy strings) in previews.

### Performance & Robustness
- Word‑boundary wrapping: `--wrap word` to reduce chopped words.
- Smarter redraws: diff rows and use `noutrefresh()/doupdate()` to lower CPU.
- Backpressure: reduce render rate at very high EPS or small terminals.

### Configuration & Packaging
- Export names: switch to `xrays_export_*.txt` and ignore in VCS.
- Defaults file: support `~/.xrays.toml` for persistent flags (pretty mode, lines per item).
- No‑color mode: `--no-color` for dumb terminals and CI outputs.
- Packaging: add `pyproject.toml` + entry point (`codex-xrays`) for `pipx` install.


## Contributing

- Issues and PRs welcome. Keep changes focused, avoid unrelated refactors.
- Style: small, focused functions; prefer predictable redraw over cleverness.
- Please test with a large/fast log stream before submitting.


## License

This repository does not include a license. If you plan to redistribute, please add an appropriate license first.
