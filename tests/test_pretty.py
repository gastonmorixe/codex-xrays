import os

import codexrays as sv


def make_app(**kwargs):
    # Use a harmless path; the app won't open it in these tests
    path = os.path.join(os.getcwd(), 'codex-tui.log')
    return sv.VizApp(None, file_path=path, **kwargs)


def test_recent_line_strips_ansi():
    app = make_app(strip_ansi=True)
    ansi = "\x1b[31mERROR\x1b[0m something happened"
    content, _attr = app.render_recent_line(ansi, width=80)
    assert "\x1b[" not in content


def test_pretty_preview_no_duplication():
    app = make_app(pretty_preview=True, pretty_mode='hybrid')
    st = sv.ItemState(item_id='msg1')
    st.type_label = 'response.output_text.delta'
    st.append_delta("The quick brown fox jumps over the lazy dog. " * 5, None, st.type_label, 0)
    lines = app.preview_lines_for_pretty(st, width=40, limit=3)
    assert 1 <= len(lines) <= 3
    if len(lines) >= 2:
        # First line is the summary; ensure the next line isn't an identical duplicate
        assert lines[0] != lines[1]
