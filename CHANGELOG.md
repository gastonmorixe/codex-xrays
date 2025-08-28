# Changelog

All notable changes to this project will be documented in this file.

## 1.0.1 — 2025-08-28

Highlights
- Pretty previews with summary + live tail, no duplication, ANSI-safe recent logs.
- Fast UI: instant ESC in full-screen, follow mode with top banner and T to jump.
- Packaging + CI: pipx entrypoint (`codex-xrays`), Ruff lint, pytest smoke tests.
- Default log path: `~/.codex/log/codex-tui.log`.
- Rename to CodeXRays 1.0.1, docs refreshed (README, AGENTS).

Commits (recent first)
- 2025-08-27 d3bacca feat(ui): follow banner and T to jump; leave follow on scroll; lower ESC delay
- 2025-08-27 bc3b777 perf(ui): ESC immediate via ESCDELAY=25ms
- 2025-08-27 58208e1 fix: default log path; README examples
- 2025-08-27 aa42fc4 docs: clarify preview behavior
- 2025-08-27 482ea11 fix(ansi): strip ANSI escapes by default; add --keep-ansi
- 2025-08-27 82b5b79 fix(pretty): summary + live tail; docs prefer codexrays.py
- 2025-08-27 9a8b094 docs: README tweak
- 2025-08-27 7c0bafa refactor: rename streamviz -> codexrays
- 2025-08-27 9ae1373 chore(lint): fix ruff issues
- 2025-08-27 39c6d83 build(ci): packaging, CI, tests, repo metadata
- 2025-08-27 01e755b feat: contributor guide and UI polish
