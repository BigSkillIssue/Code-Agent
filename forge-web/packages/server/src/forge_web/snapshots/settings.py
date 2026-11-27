"""`[snapshots]` in forge-web.toml (W66), kept apart so settings.py stays short."""

from pydantic import BaseModel, ConfigDict, Field


class SnapshotSettings(BaseModel):
    """A snapshot of every project after every run, on a storage node, and how long it stays."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    debounce_s: float = Field(default=30, ge=0)  # runs ending within this make one snapshot
    max_mb: int = Field(default=200, ge=1)  # one bundle at most; larger ones are refused
    full_every: int = Field(default=20, ge=1)  # a full bundle after this many incremental ones
    keep_all_h: float = Field(default=48, ge=0)  # every snapshot this long
    keep_daily_days: int = Field(default=30, ge=0)  # then one a day
    keep_weekly_weeks: int = Field(default=26, ge=0)  # then one a week
    fetch_timeout_s: float = Field(default=600, gt=0)  # a restore waits this long for a node
