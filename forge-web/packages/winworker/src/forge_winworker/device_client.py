"""The Windows machine's device sessions (W62), next to its builds: it asks Forge Web for a
session, starts the app in a Windows Sandbox, then hands the agent's actions to it and their
answers back, until the session ends. The same loop as the host's, which this worker does not
depend on.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import httpx

from forge_winworker.device_wire import (
    DeviceAction,
    DeviceAnswer,
    DeviceNext,
    DeviceOffer,
    DevicePoll,
)
from forge_winworker.windows_device import DeviceProblem, WindowsDevice

if TYPE_CHECKING:
    from forge_winworker.sandbox import SandboxRunner

log = logging.getLogger(__name__)
PREFIX = "/api/win/devices"


class Device(Protocol):
    """A device the agent works on."""

    async def start(self, source: Path) -> DeviceAnswer: ...
    async def act(self, action: DeviceAction) -> DeviceAnswer: ...
    async def close(self) -> None: ...


DeviceMaker = Callable[["SandboxRunner", DeviceOffer], Device]


class DeviceClient:
    """Runs this machine's device sessions."""

    def __init__(
        self, http: httpx.AsyncClient, runner: "SandboxRunner", make: DeviceMaker = WindowsDevice
    ) -> None:
        self.http = http
        self.runner = runner
        self.make = make

    async def serve(self, stop: asyncio.Event) -> None:
        """Ask for sessions until `stop` is set; network trouble only slows it down."""
        backoff = 1.0
        while not stop.is_set():
            try:
                reply = await self.http.post(f"{PREFIX}/poll")
                reply.raise_for_status()
                offer = DevicePoll.model_validate_json(reply.content).session
            except (httpx.HTTPError, ValueError) as err:
                log.warning("asking for device sessions failed: %s", err)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            backoff = 1.0
            if offer is not None:
                await self.run(offer, stop)

    async def run(self, offer: DeviceOffer, stop: asyncio.Event) -> None:
        """One session from its start to its end; whatever goes wrong ends it."""
        device = self.make(self.runner, offer)
        source = self.runner.work / "devices" / f"{offer.id}.tar.gz"
        try:
            await self.download(offer.id, source)
            first = await asyncio.wait_for(device.start(source), offer.start_timeout_s)
            await self.send(offer.id, "answer", first)
            await self.relay(offer, device, stop)
        except DeviceProblem as problem:
            await self.report_end(offer.id, str(problem), problem.hint)
        except TimeoutError:
            await self.report_end(offer.id, "the device did not start in time")
        except Exception as err:  # a bug: the server must still learn that the session is over
            log.exception("device session %s failed", offer.id)
            await self.report_end(offer.id, f"the device stopped: {err!r}"[:500])
        finally:
            await device.close()
            source.unlink(missing_ok=True)

    async def relay(self, offer: DeviceOffer, device: Device, stop: asyncio.Event) -> None:
        """The agent's actions, one by one, until the session ends or its time is up."""
        deadline = time.monotonic() + offer.max_s
        while not stop.is_set() and time.monotonic() < deadline:
            reply = await self.http.post(f"{PREFIX}/{offer.id}/next")
            if reply.status_code == 409:
                return  # the server forgot it (a restart): nothing to answer
            reply.raise_for_status()
            found = DeviceNext.model_validate_json(reply.content)
            if found.ended or (found.action is not None and found.action.kind == "stop"):
                return
            if found.action is None:
                continue
            try:
                answer = await device.act(found.action)
            except DeviceProblem as problem:
                answer = DeviceAnswer(seq=found.action.seq, ok=False, error=str(problem)[:2000],
                                      hint=problem.hint)  # fmt: skip
            if (await self.send(offer.id, "answer", answer)).get("ended"):
                return
        await self.report_end(offer.id, "the session's time is up")

    async def download(self, session_id: str, target: Path) -> None:
        """The session's packed commit."""
        target.parent.mkdir(parents=True, exist_ok=True)
        async with self.http.stream("GET", f"{PREFIX}/{session_id}/source") as reply:
            if reply.status_code != 200:
                raise DeviceProblem(
                    f"the server did not hand over the source ({reply.status_code})"
                )
            with target.open("wb") as out:
                async for chunk in reply.aiter_bytes():
                    out.write(chunk)

    async def send(self, session_id: str, what: str, answer: DeviceAnswer) -> dict[str, object]:
        """An answer (or the end) to the server."""
        reply = await self.http.post(f"{PREFIX}/{session_id}/{what}",
                                     content=answer.model_dump_json(),
                                     headers={"Content-Type": "application/json"})  # fmt: skip
        if reply.status_code == 409:
            return {"ended": True}
        reply.raise_for_status()
        found = reply.json()
        return found if isinstance(found, dict) else {}

    async def report_end(self, session_id: str, error: str, hint: str = "") -> None:
        """Tell the server the device stopped (trouble is only logged)."""
        try:
            await self.send(session_id, "ended", DeviceAnswer(seq=0, ok=False, error=error[:2000],
                                                              hint=hint[:2000]))  # fmt: skip
        except httpx.HTTPError as err:
            log.warning("reporting the end of device session %s failed: %s", session_id, err)
