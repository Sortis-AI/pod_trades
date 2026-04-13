"""Dump conversation state in a readable format."""

import json
from pathlib import Path

state_path = Path.home() / ".pod_the_trader" / "conversation.json"
if not state_path.exists():
    print("  (no state file)")
    raise SystemExit(0)

data = json.loads(state_path.read_text())
messages = data.get("messages", [])
print(f"  messages: {len(messages)}")
print(f"  trade_context: {data.get('trade_context', '')[:80]!r}")
print()
for i, m in enumerate(messages):
    role = m.get("role", "?")
    tool_calls = m.get("tool_calls")
    tool_call_id = m.get("tool_call_id", "")
    content = m.get("content") or ""
    if isinstance(content, str):
        content_preview = content[:80].replace("\n", " ")
    else:
        content_preview = f"<{type(content).__name__}>"

    if tool_calls:
        print(f"  {i:2}: {role} [{len(tool_calls)} tool_calls] {content_preview}")
        for tc in tool_calls:
            fn = tc.get("function", {}).get("name", "?")
            tc_id = tc.get("id", "?")[:30]
            print(f"        └─ {fn} id={tc_id}")
    elif role == "tool":
        print(f"  {i:2}: tool  tcid={tool_call_id[:30]} {content_preview}")
    else:
        print(f"  {i:2}: {role}: {content_preview}")
