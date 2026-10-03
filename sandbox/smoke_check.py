"""Phase 2 exit check: drive the real desktop through the executor and repeat the sequence N times.

Run from any machine that can reach the executor (for example through an SSH tunnel to 127.0.0.1:8765):

    EXECUTOR_URL=http://127.0.0.1:8765 EXECUTOR_TOKEN=... python3 sandbox/smoke_check.py [runs]

Pass criterion: at least 95% of runs complete every step. Standard library only.
"""

import json
import os
import sys
import time
import urllib.request

URL = os.environ.get("EXECUTOR_URL", "http://127.0.0.1:8765")
TOKEN = os.environ["EXECUTOR_TOKEN"]
PAGE = "file:///opt/testpage.html"


def call(path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(  # noqa: S310
        URL + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        return json.load(resp)


def act(**body) -> dict:
    return call("/action", body)


def title() -> str:
    return call("/window_title")["title"]


def one_run() -> list[str]:
    """Return the names of the steps that failed (empty list = success)."""
    failed = []
    call("/reset", {})
    act(action="key", key="ctrl+l")
    act(action="type", text=PAGE)
    act(action="key", key="Return")
    act(action="wait", seconds=1.5)
    if not title().startswith("ready"):
        failed.append("navigate")
    act(action="click", x=640, y=200)
    if "clicked" not in title():
        failed.append("click")
    act(action="click", x=640, y=530)
    act(action="type", text="hello")
    if "typed=hello" not in title():
        failed.append("type")
    act(action="scroll", direction="down", amount=10, x=640, y=400)
    if "scrolled" not in title():
        failed.append("scroll")
    return failed


def main() -> int:
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    ok = 0
    for i in range(1, runs + 1):
        start = time.time()
        failed = one_run()
        ok += not failed
        sys.stdout.write(
            f"run {i}: {'ok' if not failed else 'FAILED ' + ','.join(failed)} "
            f"({time.time() - start:.1f}s)\n"
        )
    rate = ok / runs
    sys.stdout.write(f"{ok}/{runs} passed ({rate:.0%}); need >= 95%\n")
    return 0 if rate >= 0.95 else 1


if __name__ == "__main__":
    sys.exit(main())
