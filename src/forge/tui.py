"""The terminal UI (`forge` without a prompt): conversation, live plan and a prompt line."""

from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, RichLog, Static

from forge.commands import handle_command
from forge.config import ForgeConfig
from forge.ctx import Ctx
from forge.local.tasks_screen import TasksScreen
from forge.local.tui_renderer import TuiRenderer
from forge.pipeline import PipelineError, report_text, resume, run_task
from forge.plan import checklist
from forge.ports import EventBus, Executor, Store
from forge.providers.base import ProviderError
from forge.tasks_view import list_tasks, running_count
from forge.wiring import close_session, open_session, show_events

CSS = """
#main { height: 1fr; }
#stream { width: 3fr; border: round $primary; padding: 0 1; }
#side { width: 1fr; min-width: 28; }
#plan { height: 1fr; border: round $secondary; padding: 0 1; }
#todos { height: auto; max-height: 50%; border: round $secondary; padding: 0 1; }
#prompt { dock: bottom; }
#tasksbar { dock: bottom; height: 1; color: $warning; padding: 0 1; }
#taskdetail { height: 12; border: round $secondary; }
#dialog { width: 90%; max-width: 110; height: auto; max-height: 90%; border: thick $warning;
          background: $surface; padding: 1 2; }
QuestionScreen, ApprovalScreen, TasksScreen { align: center middle; }
#preview { max-height: 20; overflow-y: auto; }
.hidden { display: none; }
"""


class ForgeApp(App[None]):
    """Forge's interactive terminal UI."""

    TITLE = "Forge"
    CSS = CSS
    BINDINGS = [Binding("ctrl+q", "quit", "Quit"), Binding("ctrl+t", "tasks", "Tasks")]

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
            with Vertical(id="side"):
                yield Static("no plan yet", id="plan")
                yield Static("no todos", id="todos")
        yield Static("", id="tasksbar")
        yield Input(placeholder="Describe a task, or /help", id="prompt")
        yield Footer()

    async def on_mount(self) -> None:
        """Open the session and start showing its events."""
        self.ctx = await open_session(self.root, self.cfg, self.renderer, **self._ports)
        events = self.ctx.bus.subscribe(self.ctx.session.id)
        self.run_worker(show_events(events, self.renderer), group="events")
        self.renderer.write(f"Forge in {self.root} — type a task, or /help", style="dim")
        self.set_interval(1.0, self.refresh_tasks)
        self.query_one("#prompt", Input).focus()

    async def refresh_tasks(self) -> None:
        """Show how many background tasks run (jobs, agents, monitors)."""
        if self.ctx is None:
            return
        running = running_count(await list_tasks(self.ctx))
        plural = "" if running == 1 else "s"
        text = f"{running} background task{plural} running — ctrl+t to view" if running else ""
        self.query_one("#tasksbar", Static).update(text)

    async def action_tasks(self) -> None:
        """Open the background-task list."""
        if self.ctx is not None:
            await self.push_screen(TasksScreen(self.ctx, await list_tasks(self.ctx)))

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
            result = await handle_command(self.ctx, text)
            if result.text:
                self.renderer.write(result.text)
            if result.prompt is not None or result.resume:
                self.start(result.prompt, resume=result.resume)
        elif self.busy:
            self.renderer.write("still working on the last task", style="yellow")
        else:
            self.start(text)

    def start(self, prompt: str | None, *, resume: bool = False) -> None:
        """Start a task (or continue the plan) unless one is running."""
        if self.busy:
            self.renderer.write("still working on the last task", style="yellow")
            return
        self.busy = True
        work = self.continue_plan() if resume else self.run_prompt(prompt or "")
        self.run_worker(work, group="task")

    async def continue_plan(self) -> None:
        """/go: run the rest of the plan."""
        assert self.ctx is not None
        try:
            plan = await resume(self.ctx)
            self.renderer.write(checklist(plan), style="bold")
        except (PipelineError, ProviderError) as exc:
            self.renderer.write(f"error: {exc}", style="bold red")
        finally:
            self.busy = False

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
