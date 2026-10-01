"""Run one real Jev task and save every decision request so it can be replayed against other models.

    uv run --env-file .env python scripts/trace_run.py --name flights
    uv run --env-file .env python scripts/trace_run.py --name wiki \
        --url https://en.wikipedia.org/wiki/Main_Page \
        --goal "Find and open the Wikipedia article about Gödel's incompleteness theorems."

Output: artifacts/traces/<name>.json with the exact TypeSafe request body of each decision, Jev's answer,
its latency and token usage. Screenshots are off; timing is the agent's own clock.
"""

import argparse
import json
import time
from pathlib import Path

from jev_ultrafast import Agent

DEFAULT_URL = "https://www.google.com/travel/flights?hl=en"
DEFAULT_GOAL = (
    "Find one-way flights from Taipei to Tokyo on November 20, 2026, for one adult in economy. "
    "Stop when matching flight options are visible. Do not select or book a flight."
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="Trace file name, e.g. flights")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--goal", action="append", help="Repeat for an ordered list of goals.")
    parser.add_argument("--out", default="artifacts/traces")
    args = parser.parse_args()
    goals = args.goal or [DEFAULT_GOAL]

    agent = Agent(args.url, goals, screenshots=False)
    try:
        for state in agent.run():
            last = state["history"][-1] if state["history"] else {}
            print(
                f"{state['elapsed_ms']:>6} ms  {len(state['history']):>2} actions  "
                f"{state['status']:<9} {last.get('action', '')}",
                flush=True,
            )
    finally:
        state = agent.snapshot()
        agent.close()

    decisions = [
        {
            "index": index,
            "request": d["request"],
            "operation": d["operation"],
            "target": d["target"],
            "confidence": d["confidence"],
            "target_confidence": d["target_confidence"],
            "latency_ms": d["latency_ms"],
            "usage": d["usage"],
            "model": d["model"],
            "elapsed_ms": d["elapsed_ms"],
        }
        for index, d in enumerate(state["decisions"], 1)
    ]
    trace = {
        "name": args.name,
        "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "url": args.url,
        "goals": goals,
        "status": state["status"],
        "final_url": state["page"]["url"],
        "elapsed_ms": state["elapsed_ms"],
        "actions": len(state["history"]),
        "text_calls": [{k: c.get(k) for k in ("model", "latency_ms", "field", "value")} for c in state["text_calls"]],
        "decisions": decisions,
    }
    out = Path(args.out) / f"{args.name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(trace, ensure_ascii=False, indent=1), encoding="utf-8")

    latencies = sorted(d["latency_ms"] for d in decisions)
    median = latencies[len(latencies) // 2] if latencies else 0
    print(
        f"\nstatus={state['status']}  actions={len(state['history'])}  decisions={len(decisions)}  "
        f"jev median={median} ms  total={state['elapsed_ms']} ms"
    )
    print(f"saved {out}")


if __name__ == "__main__":
    main()
