"""When the admins hear of new notices (SEC-W4): the report form is public, so its mail is not
one per notice but one at most per while, summing up what came in since the last one."""

import time
from dataclasses import dataclass

ADMIN_WINDOW_S = 15 * 60  # at most one mail to the admins per quarter of an hour
MAX_LISTED = 50  # notices named in one mail (the rest are counted; the panel has them all)


@dataclass(frozen=True)
class Waiting:
    """A notice the admins have not heard of yet (only what the server itself knows)."""

    id: str
    app: str
    category: str


class NoticeMail:
    """The notices waiting for the admins' next mail, and when it may go."""

    def __init__(self, window: float = ADMIN_WINDOW_S) -> None:
        self.window = window
        self.waiting: list[Waiting] = []
        self.more = 0  # waiting beyond MAX_LISTED
        self.last_sent = float("-inf")
        self.scheduled = False  # a mail is on its way; new notices join it

    def add(self, notice: Waiting) -> bool:
        """Keep a new notice; True if a mail must be scheduled for it."""
        if len(self.waiting) < MAX_LISTED:
            self.waiting.append(notice)
        else:
            self.more += 1
        if self.scheduled:
            return False
        self.scheduled = True
        return True

    def left(self) -> float:
        """Seconds until the next mail may go."""
        return max(0.0, self.last_sent + self.window - time.monotonic())

    def take(self) -> tuple[list[Waiting], int]:
        """What the mail going now names, and how many more there are."""
        taken, more = self.waiting, self.more
        self.waiting, self.more, self.scheduled = [], 0, False
        self.last_sent = time.monotonic()
        return taken, more
