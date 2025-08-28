# Repository Guidelines

Contributor guide for Codex Xrays 1.0 — a small Python TUI that tails and visualizes `codex-tui.log`. Keep changes minimal, reversible, and well‑described.

## Project Structure & Module Organization
- `streamviz.py`: Main app (tailing, parsing, rendering).
- `README.md`: Usage, keybindings, and options.
- `codex-tui.log`: Sample/active log (can be large; avoid noisy commits).
- `tests/` (when added): Pytest suite mirroring functions in `streamviz.py`.

Example:
```
./streamviz.py  ./README.md  ./codex-tui.log  [./tests/]
```

## Build, Test, and Development Commands
- Setup (optional venv): `python3 -m venv .venv && source .venv/bin/activate`.
- Run locally: `python3 streamviz.py [--from-start] [-f ./codex-tui.log] [-L 8] [--pretty-preview] [--pretty-mode hybrid|summary]`.
- Env default: `XRAYS_PRETTY=1 python3 streamviz.py` to auto-enable pretty previews.
 - Mode via env: `XRAYS_PRETTY_MODE=hybrid`.
- Windows curses: `pip install windows-curses`.
- Lint/format (recommended): `ruff check . && black .`.
- Tests (if present): `pytest -q`.

## Coding Style & Naming Conventions
- Python, 4‑space indent, prefer type hints for new/edited code.
- Names: `snake_case` for functions/vars, `CamelCase` for classes, `UPPER_CASE` for constants.
- Keep functions focused; isolate I/O (tailing, curses) from pure logic to ease testing.
- Formatting/tools: Black + Ruff; zero lint errors in commits.

## Testing Guidelines
- Framework: Pytest; place files under `tests/` as `test_*.py`.
- Target areas: `parse_sse_json`, `classify_event`, tailing edge cases (rotation/truncation), and wrapping.
- Use small, deterministic fixtures; do not parse real secrets from logs.
- Aim for ~80% line coverage on touched code. Run `pytest -q` locally.

## Commit & Pull Request Guidelines
- Commits: Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`). One logical change per commit.
- Branches: `type/short-topic` (e.g., `feat/detail-view-export`).
- PRs: clear description, linked issues (`Closes #123`), before/after notes, and a short GIF/screencast of the TUI when UI changes.
- Check list: runs locally, no lint errors, tests updated/added, README adjusted if flags/keys change.

## Security & Configuration Tips
- Logs may include sensitive content; scrub before sharing. Prefer ignoring large log files in VCS (consider adding `codex-tui.log` to `.gitignore`).
- Ensure a color‑capable terminal (`TERM=xterm-256color` or similar). On Windows, install `windows-curses`.
