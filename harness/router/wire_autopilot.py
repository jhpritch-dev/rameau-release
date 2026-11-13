"""
wire_autopilot.py -- patch main.py to route AUTOPILOT strategy through AutopilotGraph

Run from D:\Projects\bifrost-router:
    python wire_autopilot.py
"""

import pathlib

path = pathlib.Path("main.py")
content = path.read_text(encoding="utf-8")

# ── 1. Add import after existing strategy imports ──────────────────────────
old_import = "from strategies import two_pass_stream, select_local_tier"
new_import = """from strategies import two_pass_stream, select_local_tier
from autopilot_graph import run_autopilot"""

if "from autopilot_graph import run_autopilot" not in content:
    content = content.replace(old_import, new_import)
    print("✓ Added autopilot import")
else:
    print("- autopilot import already present")

# ── 2. Add autopilot handler inside the chat completions endpoint ──────────
# Insert BEFORE the existing complexity classification block.
# We detect the insertion point by looking for the band classification logic.
autopilot_block = '''
    # ── AUTOPILOT strategy ─────────────────────────────────────────────────
    x_strategy = request.headers.get("X-Strategy", "").lower()
    body_strategy = ""
    try:
        body_obj = await request.json()
        body_strategy = body_obj.get("strategy", "").upper()
    except Exception:
        body_obj = {}

    prompt_text = ""
    messages_list = []
    if body_obj:
        messages_list = body_obj.get("messages", [])
        if messages_list:
            prompt_text = messages_list[-1].get("content", "")

    if x_strategy == "autopilot" or body_strategy == "AUTOPILOT":
        import os, uuid
        from config import settings
        if not settings.anthropic_api_key:
            settings.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")

        graph_id = str(uuid.uuid4())
        try:
            result = run_autopilot(
                prompt=prompt_text,
                messages=messages_list,
                graph_id=graph_id,
            )
            assembled = result.get("assembled_output") or ""
            status = result.get("status", "UNKNOWN")
            cost = result.get("cloud_cost_usd", 0.0)
            subtask_count = len(result.get("subtasks", {}))
            completed_count = len(result.get("completed", {}))
            failed_count = len(result.get("failed", {}))

            return {
                "id": f"chatcmpl-autopilot-{graph_id[:8]}",
                "object": "chat.completion",
                "created": int(__import__("time").time()),
                "model": "bifrost-autopilot",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": assembled},
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
                "bifrost": {
                    "strategy": "AUTOPILOT",
                    "graph_id": graph_id,
                    "status": status,
                    "subtasks": subtask_count,
                    "completed": completed_count,
                    "failed": failed_count,
                    "cloud_cost_usd": cost,
                    "human_review_required": result.get("human_review_required", False),
                },
            }
        except Exception as e:
            return {
                "id": f"chatcmpl-autopilot-error",
                "object": "chat.completion",
                "created": int(__import__("time").time()),
                "model": "bifrost-autopilot",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": f"AUTOPILOT error: {str(e)[:300]}"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
    # ── END AUTOPILOT ──────────────────────────────────────────────────────
'''

# Find insertion point: after the endpoint function signature and body parsing
# Look for the complexity hint header read which is the first routing decision
insertion_marker = 'x_complexity_hint = request.headers.get("X-Complexity-Hint"'

if insertion_marker in content:
    if "AUTOPILOT strategy" not in content:
        content = content.replace(
            "    " + insertion_marker,
            autopilot_block + "\n    " + insertion_marker
        )
        print("✓ Inserted AUTOPILOT handler block")
    else:
        print("- AUTOPILOT handler already present")
else:
    # Fallback: insert before band classification
    fallback_marker = "band = "
    lines = content.split("\n")
    inserted = False
    for i, line in enumerate(lines):
        if fallback_marker in line and "classify" in content[max(0, content.find(line)-200):content.find(line)]:
            lines.insert(i, autopilot_block)
            content = "\n".join(lines)
            inserted = True
            print("✓ Inserted AUTOPILOT handler (fallback position)")
            break
    if not inserted:
        print("✗ Could not find insertion point -- manual insertion needed")
        print("  Add the autopilot_block string after the X-Complexity-Hint header read")

path.write_text(content, encoding="utf-8")
print("\nDone. Restart the Router to pick up changes:")
print("  Start-Bifrost.bat")
