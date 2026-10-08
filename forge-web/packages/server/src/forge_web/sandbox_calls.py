"""Calling a project's sandbox from an API route, with its failures as HTTP errors."""

from typing import Any

from fastapi import HTTPException

from forge_sandbox.mux import ChannelClosed
from forge_sandbox.rpc import RpcError
from forge_web.containers.driver import SandboxError
from forge_web.services import Services

# What the sandbox's expected failures mean over HTTP; anything else is the sandbox's fault.
RPC_STATUS = {
    "not_found": 404, "conflict": 409, "exists": 409, "not_empty": 409, "busy": 409,
    "git_failed": 409, "nothing_staged": 409, "too_large": 413, "invalid_path": 400,
    "is_symlink": 400, "not_a_file": 400, "not_a_directory": 400, "bad_params": 400,
}  # fmt: skip


async def sandbox_call(
    services: Services, project_id: str, method: str, params: dict[str, Any] | None = None
) -> Any:
    """The result of one sandbox method; HTTP errors for failures."""
    try:
        return await services.runs.call(project_id, method, params or {})
    except RpcError as err:
        raise HTTPException(RPC_STATUS.get(err.code, 502), err.message) from None
    except (ChannelClosed, SandboxError, OSError, TimeoutError):
        raise HTTPException(503, "the project's sandbox is not reachable") from None


def result_dict(value: Any) -> dict[str, Any]:
    """A sandbox result that must be an object (the sandbox is not trusted)."""
    if not isinstance(value, dict):
        raise HTTPException(502, "the sandbox sent an unexpected answer")
    return value
