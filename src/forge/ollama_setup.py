"""Set up a local Ollama model for Forge (`forge ollama setup|status`), S57.

Ollama gives models a small context window by default and cuts longer prompts silently;
Forge's system prompt and tool schemas alone need several thousand tokens. Setup therefore
creates a derived model `forge-<model>` with a large enough `num_ctx`, which then works
through Ollama's ordinary OpenAI-compatible endpoint.
"""

import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from forge.config import DEFAULT_ROLES, ForgeConfig
from forge.config_edit import parse_toml, replace_table
from forge.providers.catalog import PRESETS

VARIANT_PREFIX = "forge-"
SKIPPED_ROLES = frozenset({"browser"})  # needs a vision model; the suggested models are text-only
TIMEOUT = httpx.Timeout(30.0, read=None)  # pulls and model loads can take minutes


@dataclass
class Hardware:
    """What the machine offers a local model."""

    ram_gb: float
    vram_gb: float = 0.0  # dedicated GPU memory (NVIDIA)
    apple: bool = False  # Apple silicon: the GPU shares the RAM

    def model_memory_gb(self) -> float:
        """Memory a model can run in quickly: the GPU's, or most of an Apple chip's RAM."""
        return self.vram_gb or (self.ram_gb * 0.75 if self.apple else 0.0)


@dataclass
class Choice:
    """The suggested model, its context window and why."""

    model: str
    context: int
    reason: str


@dataclass
class ServerStatus:
    """A running Ollama server."""

    version: str
    models: list[str]


def recommend(hardware: Hardware) -> Choice:
    """The largest model that runs at a usable speed on this hardware."""
    memory = hardware.model_memory_gb()
    if memory >= 24:
        return Choice("qwen3-coder:30b", 32768, f"{memory:.0f} GB fast memory: a 30B coding model")
    if memory >= 12:
        return Choice("qwen3:14b", 32768, f"{memory:.0f} GB fast memory: a 14B model")
    if memory >= 7:
        return Choice("qwen3:8b", 32768, f"{memory:.0f} GB fast memory: an 8B model")
    context = 16384 if hardware.ram_gb >= 12 else 8192
    return Choice(
        "qwen3:4b-instruct",
        context,
        f"no GPU found, {hardware.ram_gb:.0f} GB RAM: a small model without a thinking phase "
        "(slow, and weak on hard tasks)",
    )


def detect_hardware() -> Hardware:
    """RAM, NVIDIA GPU memory and Apple silicon, without extra packages."""
    apple = sys.platform == "darwin" and platform.machine() == "arm64"
    return Hardware(ram_gb=ram_gb(), vram_gb=nvidia_vram_gb(), apple=apple)


def ram_gb() -> float:
    """Total physical memory in GB (0 when unknown)."""
    if sys.platform == "win32":
        return windows_ram_gb()
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3
    except (ValueError, OSError, AttributeError):
        return 0.0


def windows_ram_gb() -> float:
    """Total memory via GlobalMemoryStatusEx."""

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("load", ctypes.c_ulong),
            ("total_phys", ctypes.c_ulonglong),
            *[(name, ctypes.c_ulonglong) for name in ("a", "b", "c", "d", "e", "f")],
        ]

    status = MemoryStatus()
    status.length = ctypes.sizeof(MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined,unused-ignore]  # windll exists only on Windows
        return 0.0
    return float(status.total_phys) / 1024**3


def nvidia_vram_gb() -> float:
    """Memory of the largest NVIDIA GPU, from nvidia-smi (0 without one)."""
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return 0.0
    try:
        out = subprocess.run(
            [exe, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    sizes = [float(line) for line in out.split() if line.replace(".", "", 1).isdigit()]
    return max(sizes, default=0.0) / 1024


def api_root(cfg: ForgeConfig) -> str:
    """Ollama's native API URL, from the `ollama` provider's OpenAI-compatible base URL."""
    provider = cfg.providers.get("ollama") or PRESETS["ollama"]
    base = (provider.base_url or "http://localhost:11434/v1").rstrip("/")
    return base.removesuffix("/v1")


async def server_status(api: str) -> ServerStatus | None:
    """The running server's version and installed models, or None if it does not answer."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            version = (await client.get(f"{api}/api/version")).json()["version"]
            tags = (await client.get(f"{api}/api/tags")).json()
    except (httpx.HTTPError, ValueError, KeyError):
        return None
    return ServerStatus(str(version), [str(m["name"]) for m in tags.get("models", [])])


async def pull(api: str, model: str, show: Callable[[str], None]) -> None:
    """Download a model, reporting progress lines; raises ValueError on Ollama errors."""
    body = {"model": model, "stream": True}
    async with (
        httpx.AsyncClient(timeout=TIMEOUT) as client,
        client.stream("POST", f"{api}/api/pull", json=body) as response,
    ):
        async for line in response.aiter_lines():
            if line.strip():
                show(progress_text(json.loads(line)))


def progress_text(event: dict[str, Any]) -> str:
    """One readable progress line from a pull event."""
    if "error" in event:
        raise ValueError(f"ollama: {event['error']}")
    status = str(event.get("status", ""))
    total, done = event.get("total"), event.get("completed")
    if total and done is not None:
        return f"{status} {int(done * 100 / total)}%"
    return status


async def create_variant(api: str, model: str, context: int) -> str:
    """Create `forge-<model>` with the given context window; returns its name."""
    name = VARIANT_PREFIX + model
    body = {"model": name, "from": model, "parameters": {"num_ctx": context}, "stream": False}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.post(f"{api}/api/create", json=body)
    if response.status_code >= 400:
        raise ValueError(f"ollama could not create {name}: {response.text[:300]}")
    return name


async def model_capabilities(api: str, model: str) -> list[str]:
    """What Ollama says the model can do ('tools', 'vision', 'thinking', ...)."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.post(f"{api}/api/show", json={"model": model})
    if response.status_code >= 400:
        return []
    return [str(c) for c in response.json().get("capabilities", [])]


def write_setup(path: Path, model: str, context: int, tools: bool) -> None:
    """Point every text role at `ollama/<model>` and record its context window."""
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    roles = dict(parse_toml(text, path).get("roles") or {})
    chain = [f"ollama/{model}"]
    roles.update({role: chain for role in DEFAULT_ROLES if role not in SKIPPED_ROLES})
    text = replace_table(text, ["roles"], roles)
    entry = {"context_window": context, "tools": tools, "vision": False}
    text = replace_table(
        text, ["models", f"ollama/{model}"], {**entry, "cost_in": 0.0, "cost_out": 0.0}
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def context_warnings(cfg: ForgeConfig) -> list[str]:
    """Ollama models in use whose context window Forge does not know (likely truncated)."""
    models = sorted({m for chain in cfg.roles.values() for m in chain if m.startswith("ollama/")})
    unknown = [
        m
        for m in models
        if (cfg.models.get(m) or None) is None or cfg.models[m].context_window is None
    ]
    return [
        f"{m} uses Ollama's small default context, so long prompts are cut; "
        "run `forge ollama setup`"
        for m in unknown
        if not m.removeprefix("ollama/").startswith(VARIANT_PREFIX)
    ]
