import json
import os

import codexrays as sv


def sse_line(typ, item_id, out_idx, delta):
    payload = {
        "type": typ,
        "item_id": item_id,
        "output_index": out_idx,
        "delta": delta,
    }
    return f"SSE event: {json.dumps(payload)}"


def test_new_since_counts_new_items_only():
    app = sv.VizApp(None, file_path=os.path.join(os.getcwd(), "codex-tui.log"))
    app.follow_top = False
    # First new item -> count 1
    app.handle_line(sse_line("response.output_text.delta", "idA", 0, "hi"))
    assert app.new_since == 1
    # Another delta on same item -> should NOT increment
    app.handle_line(sse_line("response.output_text.delta", "idA", 0, " there"))
    assert app.new_since == 1
    # New item -> increments to 2
    app.handle_line(sse_line("response.output_text.delta", "idB", 0, "hey"))
    assert app.new_since == 2
