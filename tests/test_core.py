import os
import codexrays as sv


def test_parse_sse_json_basic():
    line = 'SSE event: {"type":"response.output_text.delta","item_id":"abc","output_index":0,"delta":"Hello"}'
    data = sv.parse_sse_json(line)
    assert data and data["type"].endswith("output_text.delta")
    assert data["item_id"] == "abc"
    assert data["delta"] == "Hello"


def test_summarize_apply_patch_diffstat():
    patch = (
        "*** Begin Patch\n"
        "*** Add File: foo.txt\n"
        "+Hello\n"
        "*** Update File: bar.py\n"
        "@@\n- old\n+ new\n"
        "*** End Patch\n"
    )
    # Build a fake command string that contains the patch envelope
    cmd = f"apply_patch << 'PATCH'\n{patch}PATCH"
    app = sv.VizApp(None, file_path=os.path.join(os.getcwd(), 'codex-tui.log'))
    out = app._summarize_apply_patch(cmd, width=80)
    assert out is not None
    # Has counts and filenames
    assert "+" in out and "✏️" in out
    assert "foo.txt" in out or "bar.py" in out
