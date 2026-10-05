"""The terminal UI (`forge` without a prompt): conversation, live plan and a prompt line."""

from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, Header, Input, RichLog, Static

from forge.commands import run_command
from forge.config import ForgeConfig
from forge.ctx import Ctx
from forge.local.tui_renderer import TuiRenderer
from forge.pipeline import PipelineError, report_text, run_task
from forge.ports import EventBus, Executor, Store
from forge.providers.base import ProviderError
from forge.wiring import close_session, open_session, show_events

CSS = """
#main { height: 1fr; }
#stream { width: 3fr; border: round $primary; padding: 0 1; }
#plan { width: 1fr; min-width: 28; border: round $secondary; padding: 0 1; }
#prompt { dock: bottom; }
#dialog { width: 90%; max-width: 110; height: auto; max-height: 90%; border: thick $warning;
          background: $surface; padding: 1 2; }
QuestionScreen, ApprovalScreen { align: center middle; }
#preview { max-height: 20; overflow-y: auto; }
.hidden { display: none; }
"""


class ForgeApp(App[None]):
    """Forge's interactive terminal UI."""

    TITLE = "Forge"
    CSS = CSS
    BINDINGS = [Binding("ctrl+q", "quit", "Quit")]

    def __init__(
        self,
        root: Path,
        cfg: ForgeConfig,
        *,
        store: Store | None = None,
        bus: EventBus | None = None,
        executor: Executor | None = None,
    ) -> None:
        super().__init__()
        self.root, self.cfg = root, cfg
        self._ports: dict[str, Any] = {"store": store, "bus": bus, "executor": executor}
        self.renderer = TuiRenderer(self)
        self.ctx: Ctx | None = None
        self.busy = False

    def compose(self) -> ComposeResult:
        """Header, conversation and plan side by side, prompt line, footer."""
        yield Header()
        with Horizontal(id="main"):
            yield RichLog(id="stream", wrap=True, markup=False)
            yield Static("no plan yet", id="plan")
        yield Input(placeholder="Describe a task, or /help", id="prompt")
        yield Footer()

    async def on_mount(self) -> None:
        """Open the session and start showing its events."""
        self.ctx = await open_session(self.root, self.cfg, self.renderer, **self._ports)
        events = self.ctx.bus.subscribe(self.ctx.session.id)
        self.run_worker(show_events(events, self.renderer), group="events")
        self.renderer.write(f"Forge in {self.root} — type a task, or /help", style="dim")
        self.query_one("#prompt", Input).focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Run a slash command, or start a task."""
        if event.input.id != "prompt":
            return
        text = event.value.strip()
        event.input.value = ""
        if not text or self.ctx is None:
            return
        if text.startswith("/"):
            self.renderer.write(text, style="bold")
            self.renderer.write(await run_command(self.ctx, text))
        elif self.busy:
            self.renderer.write("still working on the last task", style="yellow")
        else:
            self.busy = True
            self.run_worker(self.run_prompt(text), group="task")

    async def run_prompt(self, text: str) -> None:
        """Work on one task and show the report."""
        assert self.ctx is not None
        self.renderer.write(f"> {text}", style="bold")
        try:
            report = await run_task(text, self.ctx)
            self.renderer.write(
                report_text(report), style="bold green" if report.ok else "bold red"
            )
        except (PipelineError, ProviderError) as exc:
            self.renderer.write(f"error: {exc}", style="bold red")
        finally:
            self.busy = False

    async def on_unmount(self) -> None:
        """Stop the session's jobs and shells."""
        if self.ctx is not None:
            await close_session(self.ctx)


def run_tui(root: Path, cfg: ForgeConfig) -> int:
    """Open the TUI until the user quits."""
    ForgeApp(root, cfg).run()
    return 0
