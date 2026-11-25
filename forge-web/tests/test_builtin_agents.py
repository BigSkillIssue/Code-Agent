"""The agents Forge Web ships (W52): Forge reads them as its own agent files, and the UI agent
works from the product's DESIGN.md and tokens."""

from pathlib import Path

from forge.agent_files import parse_agent_file

from forge_sandbox.products.render import product_files
from forge_sandbox.prompts import BUILTIN_AGENTS, agent_file


def test_every_builtin_agent_is_a_valid_forge_agent_file(tmp_path: Path) -> None:
    names = [agent.name for agent in BUILTIN_AGENTS]
    assert names[:3] == ["ui", "tester", "security"] and len(set(names)) == len(names)
    for agent in BUILTIN_AGENTS:
        path = tmp_path / f"{agent.name}.md"
        path.write_text(agent_file(agent.name, agent.description, agent.prompt,
                                   ["anthropic/claude-sonnet"], ["read", "edit"]))  # fmt: skip
        parsed = parse_agent_file(path)
        assert (parsed.name, parsed.models, parsed.tools) == (
            agent.name, ["anthropic/claude-sonnet"], ["read", "edit"],
        )  # fmt: skip
        assert parsed.prompt.startswith("You are the")
        assert agent.policy in ("required", "recommended")
        assert agent.applies_to in ("always", "product", "ui")


def test_the_ui_agent_designs_from_the_subject_and_looks_at_its_work() -> None:
    ui = next(agent for agent in BUILTIN_AGENTS if agent.name == "ui")
    for rule in ("docs/DESIGN.md", "styles.css", "390 px", "product_look", "cream", "all-caps"):
        assert rule in ui.prompt, rule


def test_products_start_with_tokens_and_a_design_page() -> None:
    for kind in ("fullstack", "site"):
        files = product_files(kind, "tally", None, (), "apps.example.net")
        css = files["web/src/styles.css"].decode()
        for token in ("--paper", "--ink", "--accent", "--step-0", "prefers-color-scheme: dark",
                      ":focus-visible", "prefers-reduced-motion"):  # fmt: skip
            assert token in css, (kind, token)
        assert b"## Palette" in files["docs/DESIGN.md"]
