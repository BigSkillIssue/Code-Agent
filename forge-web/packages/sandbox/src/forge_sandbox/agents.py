"""The admin's agents in a chat's sandbox (W53): written as Forge agent files into the worker's
Forge folder (`~/.forge/agents`), where Forge's lead finds them by name. Files Forge Web wrote
before and the admin removed since are taken away again; files of the project stay untouched."""

import json
from pathlib import Path

from forge_sandbox.methods import AgentSpec
from forge_sandbox.prompts import agent_file

WRITTEN = ".forge-web-agents.json"  # the files this module wrote, so it can remove them later
POLICY_NOTE = {
    "required": "Forge Web runs it on every task it applies to.",
    "recommended": "Recommended by the admin for work in its field.",
}


def write_agent_files(home: Path, agents: list[AgentSpec]) -> list[str]:
    """Write the agents' files; return their names."""
    folder = home / "agents"
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / WRITTEN
    try:
        before = set(json.loads(marker.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        before = set()
    names = [agent.name for agent in agents]
    for gone in sorted(before - set(names)):
        (folder / f"{gone}.md").unlink(missing_ok=True)
    for agent in agents:
        description = f"{agent.description} {POLICY_NOTE[agent.policy]}".strip()
        text = agent_file(agent.name, description, agent.prompt, agent.models, agent.tools)
        (folder / f"{agent.name}.md").write_text(text, encoding="utf-8")
    marker.write_text(json.dumps(names), encoding="utf-8")
    return names
