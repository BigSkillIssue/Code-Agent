"""Custom agents from agent files: `.forge/agents/<name>.md` and `~/.forge/agents/<name>.md`.

Front matter between `---` lines (`name`, `model`, `tools`, `description`), then the prompt.
Project files win over user files with the same name.
"""

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from forge.config import forge_home
from forge.ctx import Ctx, CustomRole
from forge.runtime.errors import ToolError
from forge.tools import REGISTRY, TOOL_GROUPS

NAME = re.compile(r"^[a-z0-9-]{1,32}$")


AGENT_KEYS = frozenset({"name", "model", "tools", "description"})


class AgentFileError(ValueError):
    """An agent file that cannot be used; the message names the file."""


@dataclass
class AgentDef:
    """A custom role from `.forge/agents/<name>.md` or `~/.forge/agents/<name>.md`."""

    name: str
    models: list[str]  # fallback chain; empty = the role's configured or default models
    tools: list[str] | None  # tool or group names; None = the coder's tools
    description: str
    prompt: str
    path: Path


def agent_dirs(root: Path) -> list[Path]:
    """User folder first, project folder last (so project files win on a name clash)."""
    return [forge_home() / "agents", root / ".forge" / "agents"]


def load_agents(root: Path) -> dict[str, AgentDef]:
    """Every agent file of the user and the project, by name."""
    agents: dict[str, AgentDef] = {}
    for folder in agent_dirs(root):
        for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
            agent = parse_agent_file(path)
            agents[agent.name] = agent
    return agents


def parse_agent_file(path: Path) -> AgentDef:
    """Front matter between `---` lines (name, model, tools, description), then the prompt."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if not text.startswith("---\n") or "\n---" not in text[3:]:
        raise AgentFileError(f"{path}: must start with front matter between '---' lines")
    head, _, body = text[4:].partition("\n---")
    fields = parse_front_matter(head, path)
    name = fields.get("name") or path.stem
    if not isinstance(name, str) or not NAME.match(name):
        raise AgentFileError(f"{path}: name must match [a-z0-9-]{{1,32}}, got {name!r}")
    prompt = body.partition("\n")[2].strip()
    if not prompt:
        raise AgentFileError(f"{path}: the prompt (text after the front matter) is empty")
    return AgentDef(
        name=name,
        models=as_list(fields.get("model")),
        tools=as_list(fields["tools"]) if "tools" in fields else None,
        description=str(fields.get("description") or ""),
        prompt=prompt,
        path=path,
    )


def parse_front_matter(head: str, path: Path) -> dict[str, str | list[str]]:
    """`key: value` lines; lists as `[a, b]`, `a, b` or following `- item` lines."""
    fields: dict[str, str | list[str]] = {}
    key = ""
    for number, line in enumerate(head.splitlines(), start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and key:
            current = fields.get(key)
            fields[key] = [
                *(current if isinstance(current, list) else []),
                line.strip()[2:].strip(),
            ]
            continue
        key, colon, value = line.partition(":")
        key = key.strip()
        if not colon or key not in AGENT_KEYS:
            raise AgentFileError(
                f"{path}: line {number}: expected 'key: value' with key "
                f"{', '.join(sorted(AGENT_KEYS))}"
            )
        fields[key] = value.strip().strip("[]").strip()
    return fields


def as_list(value: str | list[str] | None) -> list[str]:
    """A comma-separated value or list, without blanks or quotes."""
    items = value if isinstance(value, list) else (value or "").split(",")
    return [i.strip().strip("'\"") for i in items if i.strip()]


async def use_agent_file(ctx: Ctx, role: str) -> None:
    """Install the agent file named `role` (if any) into the session: models, tools, prompt."""
    try:
        agents = await asyncio.to_thread(load_agents, ctx.root)
    except (AgentFileError, OSError, UnicodeDecodeError) as err:
        raise ToolError("invalid_args", f"bad agent file: {err}") from err
    agent = agents.get(role)
    if agent is None:
        return
    unknown = [t for t in agent.tools or [] if t not in REGISTRY and t not in TOOL_GROUPS]
    if unknown:
        raise ToolError("invalid_args", f"{agent.path}: unknown tools {', '.join(unknown)}")
    if agent.models:
        ctx.cfg.roles[agent.name] = agent.models
    ctx.state.custom_roles[agent.name] = CustomRole(agent.prompt, agent.tools, agent.description)
