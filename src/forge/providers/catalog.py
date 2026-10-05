"""Known models and their capabilities; config `[models.*]` entries override them."""

from forge.config import ModelOverride
from forge.providers.base import Capabilities

KNOWN_MODELS: dict[str, Capabilities] = {}


def capabilities_for(
    provider: str, model: str, overrides: dict[str, ModelOverride] | None = None
) -> Capabilities:
    """Capabilities of `provider/model`: catalog entry (or safe defaults) plus overrides."""
    caps = KNOWN_MODELS.get(model, Capabilities())
    override = (overrides or {}).get(f"{provider}/{model}")
    if override is None:
        return caps
    changes = {k: v for k, v in override.model_dump().items() if v is not None}
    return caps.model_copy(update=changes)
