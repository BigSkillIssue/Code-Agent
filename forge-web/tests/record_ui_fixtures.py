"""Record chats for the web UI's tests: `uv run python tests/record_ui_fixtures.py`.

Starts development servers with Forge's fake model and a script each, drives a chat through
them, and writes the chat's stored items to `frontend/src/fixtures/` — exactly what the UI gets:

- `chat-tools.json`: a small task in "ask" mode: todos, approvals, a command, an edit, a question.
- `chat-plan.json`: a medium task in "edits" mode: the plan, its approval, steps, a sub-agent,
  the final review.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from support import LiveServer, call, dev_settings, fake_script

FIXTURES = Path(__file__).parents[1] / "frontend" / "src" / "fixtures"
TODOS = [
    {"content": "Write hello.py", "status": "completed", "active_form": "Writing hello.py"},
    {"content": "Run it", "status": "in_progress", "active_form": "Running it"},
    {"content": "Polish the greeting", "status": "pending", "active_form": "Polishing"},
]
QUESTION = {"text": "Add a test as well?", "kind": "confirm", "default": "no",
            "why": "It decides how much to change."}  # fmt: skip
GREET = "def greet(name):\n    return f'Hi {name}'\n"
FINAL = (
    "Done. **hello.py** now prints `hello, world`.\n\n"
    '- written and run\n- checked by a sub-agent\n\n```python\nprint("hello, world")\n```'
)


def script() -> dict[str, Any]:
    """The fake model's turns: the main agent's, and the explore sub-agent's."""
    data = fake_script(
        call("todo_write", todos=TODOS),
        call("write_file", path="hello.py", content='print("hello")\n'),
        call("bash", command="python3 hello.py", description="Run the script"),
        call("edit_file", path="hello.py", old="hello", new="hello, world"),
        call("ask_user", questions=[QUESTION]),
        call("todo_write", todos=[{**t, "status": "completed"} for t in TODOS]),
        {"text": FINAL},
    )
    return data


def plan_script() -> dict[str, Any]:
    """A medium task: refiner, planner, two steps (one with a sub-agent), reviewer."""
    spec = {
        "goal": "Add a greeting module", "context": "", "requirements": ["greet(name)"],
        "constraints": [], "acceptance_criteria": ["greet.py exists"], "assumptions": [],
        "open_questions": [], "size": "medium",
    }  # fmt: skip
    steps = [
        {"title": "Write greet.py", "detail": "A greet(name) function.", "files": ["greet.py"],
         "check": "test -f greet.py"},
        {"title": "Check the module", "detail": "Let a sub-agent look.", "depends_on": [1],
         "check": "test -f greet.py"},
    ]  # fmt: skip
    check = "Run python3 -c 'import greet; print(greet.greet(\"Ada\"))'"
    review = {"ok": True, "summary": "greet.py greets by name; both steps are done.",
              "manual_checks": [check]}  # fmt: skip
    return {
        "roles": {
            "refiner": [{"text": json.dumps(spec)}],
            "planner": [call("submit_plan", steps=steps, explanation="Write it, then check it."),
                        {"text": "Plan submitted."}],
            "explore": [call("list_dir", path="."), {"text": "greet.py is there."}],
            "reviewer": [{"text": json.dumps(review)}],
        },
        "turns": [
            call("write_file", path="greet.py", content=GREET),
            call("finish_step", step_id="s1", summary="Wrote greet.py."),
            {"text": "Step 1 done."},
            call("spawn_agent", role="explore", task="Check that greet.py exists."),
            call("finish_step", step_id="s2", summary="A sub-agent checked greet.py."),
            {"text": "Step 2 done."},
        ],
    }  # fmt: skip


def answer_for(item: dict[str, Any]) -> dict[str, Any]:
    """What the recorded user answers."""
    if item["kind"] == "approval":
        return {"allow": True, "remember": False, "feedback": ""}
    return {"answers": [{"question_index": 0, "values": ["no"]}]}


async def drive(client: httpx.AsyncClient, chat_id: str, prompt: str) -> list[dict[str, Any]]:
    """Send the prompt and answer every request until the turn ends; the stored items."""
    sent = await client.post(f"/api/chats/{chat_id}/messages", json={"text": prompt})
    sent.raise_for_status()
    answered: set[str] = set()
    async with asyncio.timeout(120):
        while True:
            items = (await client.get(f"/api/chats/{chat_id}/events")).json()["items"]
            for entry in items:
                item = entry["item"]
                if item["type"] == "request" and item["id"] not in answered:
                    answered.add(item["id"])
                    body = {"request_id": item["id"], "answer": answer_for(item)}
                    (
                        await client.post(f"/api/chats/{chat_id}/answer", json=body)
                    ).raise_for_status()
            if any(entry["item"]["type"] == "turn" for entry in items):
                return list(items)
            await asyncio.sleep(0.2)


async def record(server: LiveServer, mode: str, prompt: str, name: str) -> None:
    """Run one chat on `server` and write its items to the fixture `name`."""
    async with httpx.AsyncClient(
        base_url=server.url, headers=server.headers(), timeout=60
    ) as client:
        project = (await client.post("/api/projects", json={"name": "Demo"})).json()
        chat = (
            await client.post(f"/api/projects/{project['id']}/chats", json={"mode": mode})
        ).json()
        items = await drive(client, chat["id"], prompt)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    out = FIXTURES / name
    out.write_text(json.dumps({"items": items}, indent=1) + "\n", encoding="utf-8")
    kinds = [e["item"].get("event", {}).get("kind", e["item"]["type"]) for e in items]
    print(f"wrote {out} ({len(items)} items): {', '.join(kinds)}")


def main() -> None:
    runs = [
        (script(), "ask", "Write hello.py and run it", "chat-tools.json"),
        (plan_script(), "edits", "Add a greeting module", "chat-plan.json"),
    ]
    for data, mode, prompt, name in runs:
        with (
            tempfile.TemporaryDirectory() as folder,
            LiveServer(dev_settings(Path(folder) / "data", data)) as server,
        ):
            asyncio.run(record(server, mode, prompt, name))


if __name__ == "__main__":
    main()
