"""Pretty-print ADK event JSON lines (stdin): tool calls, results, text, errors, token usage."""
import json
import sys

for line in sys.stdin:
    line = line.strip()
    if line.startswith("data:"):
        line = line[5:].strip()
    if not line:
        continue
    try:
        e = json.loads(line)
    except Exception:
        print("RAW", line[:300])
        continue
    for p in (e.get("content") or {}).get("parts", []):
        if "function_call" in p:
            print("CALL  ", p["function_call"]["name"], json.dumps(p["function_call"].get("args")))
        elif "function_response" in p:
            print("RESULT", p["function_response"]["name"], str(p["function_response"].get("response"))[:200])
        elif "text" in p and not p.get("thought"):
            print("TEXT  ", p["text"])
    if e.get("error_code"):
        print("ERROR ", e.get("error_code"), e.get("error_message"))
    u = e.get("usage_metadata")
    if u:
        print("USAGE ", u.get("prompt_token_count"), "in /", u.get("candidates_token_count"), "out")
