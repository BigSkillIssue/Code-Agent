"""The TUI's background-task list (ctrl+t): pick a task to see its output, `s` stops it."""

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList, RichLog

from forge.ctx import Ctx
from forge.tasks_view import TaskRow, list_tasks, row_text, stop_task, task_detail


class TasksScreen(ModalScreen[None]):
    """Background jobs, agents and monitors; Enter shows one, `s` stops it, Esc closes."""

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Close"),
        Binding("s", "stop", "Stop task"),
        Binding("r", "reload", "Refresh"),
    ]

    def __init__(self, ctx: Ctx, rows: list[TaskRow]) -> None:
        super().__init__()
        self.ctx = ctx
        self.rows = rows

    def compose(self) -> ComposeResult:
        """Title, the task list and the selected task's output."""
        with Vertical(id="dialog"):
            yield Label(Text("Background tasks  (Enter: show, s: stop, r: refresh, Esc: close)"))
            yield OptionList(*self.options(), id="tasklist")
            yield RichLog(id="taskdetail", wrap=True, markup=False)

    def options(self) -> list[str]:
        """One line per task."""
        return [row_text(row) for row in self.rows] or ["no background tasks"]

    def selected(self) -> TaskRow | None:
        """The highlighted task."""
        index = self.query_one("#tasklist", OptionList).highlighted
        return self.rows[index] if index is not None and index < len(self.rows) else None

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Show the picked task's output."""
        if event.option_index < len(self.rows):
            await self.show_text(await task_detail(self.ctx, self.rows[event.option_index].id))

    async def action_stop(self) -> None:
        """Stop the highlighted task."""
        row = self.selected()
        if row is not None:
            await self.show_text(await stop_task(self.ctx, row.id))
            await self.action_reload()

    async def action_reload(self) -> None:
        """Read the task list again."""
        self.rows = await list_tasks(self.ctx)
        tasks = self.query_one("#tasklist", OptionList)
        tasks.clear_options()
        tasks.add_options(self.options())

    def action_dismiss_screen(self) -> None:
        """Close the list."""
        self.dismiss(None)

    async def show_text(self, text: str) -> None:
        """Replace the detail pane's text."""
        log = self.query_one("#taskdetail", RichLog)
        log.clear()
        log.write(Text(text))
