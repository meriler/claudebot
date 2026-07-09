"""A fake `claude --input-format stream-json` for StreamingSession tests.

Deterministic stream-json echo server (NOT a model). It exercises the engine's
mechanics — stdin delivery, stdout dispatch, turn resolution on `result`,
mid-turn injection, interrupt, process death — without needing the real CLI.

Protocol:
  user message text T:
    -> emit assistant "ACK:T"
    -> if T contains "HOLD": emit NO result (turn stays open, awaiting more
       input — models a turn in progress that an inject/interrupt can steer).
       "contains" not "startswith" because a fresh session prefixes the first
       message with the system/mode prompt.
    -> if T contains "TAIL": emit an EMPTY result immediately, then keep
       talking — assistant "TAILTEXT:T" and a second result. Models the
       background-task pattern (CLI 2.1.x): the visible turn ends early
       while task-notification wake-ups re-enter the agent loop.
    -> else: emit result "RESULT:T"
  control_request interrupt:
    -> emit control_response success
    -> emit result (error_during_execution) "INTERRUPTED"
"""

import json
import sys


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    while True:
        line = sys.stdin.readline()
        if not line:  # EOF — parent closed stdin
            return
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "control_request":
            rid = ev.get("request_id")
            emit(
                {"type": "control_response", "response": {"subtype": "success", "request_id": rid}}
            )
            emit(
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "result": "INTERRUPTED",
                    "session_id": "fake-sid-1",
                }
            )
        elif etype == "user":
            text = ev["message"]["content"][0]["text"]
            emit(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": f"ACK:{text}"}]},
                }
            )
            if "TAIL" in text:
                # Early empty result: the visible turn is over for the client…
                emit(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": "",
                        "session_id": "fake-sid-1",
                    }
                )
                # …but the process keeps working and talking (turn tail).
                emit(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": f"TAILTEXT:{text}"}]},
                    }
                )
                emit(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": f"RESULT:TAIL-DONE:{text}",
                        "session_id": "fake-sid-1",
                    }
                )
            elif "HOLD" not in text:
                emit(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": f"RESULT:{text}",
                        "session_id": "fake-sid-1",
                    }
                )


if __name__ == "__main__":
    main()
