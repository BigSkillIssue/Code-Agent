"""Forge: a provider-agnostic coding agent."""

from typing import Any

__version__ = "0.1.0"
__all__ = ["Forge", "__version__"]


def __getattr__(name: str) -> Any:
    """`from forge import Forge` loads the API only when it is used (keeps the CLI fast)."""
    if name == "Forge":
        from forge.api import Forge

        return Forge
    raise AttributeError(f"module 'forge' has no attribute {name!r}")
