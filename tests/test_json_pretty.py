import codexrays as sv


def test_pretty_json_lines_success():
    app = sv.VizApp(None, file_path="codex-tui.log", json_pretty=True)
    src = '{"a": 1, "b": {"c": "d"}}'
    lines = app._pretty_json_lines(src)
    assert lines and lines[0].strip().startswith("{") and "  " in lines[1]


def test_pretty_json_lines_invalid_returns_none():
    app = sv.VizApp(None, file_path="codex-tui.log", json_pretty=True)
    assert app._pretty_json_lines("not json") is None
