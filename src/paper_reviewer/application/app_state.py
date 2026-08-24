from __future__ import annotations

import json
import os
from pathlib import Path

from platformdirs import user_data_path
from pydantic import BaseModel, Field, field_validator

# Run states that represent a run which can still make progress.  Keep these
# values as strings rather than importing ``RunStatus`` here: the GUI is also
# used while reading databases created by an older/newer build and must not
# fail to start merely because a state was added to the domain enum.
ACTIVE_RUN_STATUS_VALUES = frozenset(
    {
        "created",
        "ingesting",
        "ingested",
        "building_evidence",
        "evidence_ready",
        "scoring",
        "reviewing",
        "auditing",
        "awaiting_hard_rule_confirmation",
        "panel_reviewing",
        "supplemental_reviewing",
        "awaiting_panel_review",
        "synthesizing",
        "meta_reviewing",
        "validating",
    }
)

HUMAN_REVIEW_STATUS_VALUES = frozenset(
    {"awaiting_hard_rule_confirmation", "awaiting_panel_review"}
)

TERMINAL_RUN_STATUS_VALUES = frozenset(
    {"reported", "fatal_failure", "cancelled"}
)


def status_value(status: object) -> str:
    """Return a stable string for a domain status or a raw event status."""

    value = getattr(status, "value", status)
    return value if isinstance(value, str) else str(value)


def is_active_run_status(status: object) -> bool:
    return status_value(status) in ACTIVE_RUN_STATUS_VALUES


def is_human_review_status(status: object) -> bool:
    return status_value(status) in HUMAN_REVIEW_STATUS_VALUES


def is_terminal_run_status(status: object) -> bool:
    return status_value(status) in TERMINAL_RUN_STATUS_VALUES


class AppPaths(BaseModel):
    root: Path
    data_dir: Path
    runs_dir: Path
    logs_dir: Path
    config_dir: Path

    @classmethod
    def for_current_user(cls) -> AppPaths:
        root = user_data_path("PaperReviewer", "PaperReviewer", roaming=False)
        return cls(
            root=root,
            data_dir=root / "data",
            runs_dir=root / "runs",
            logs_dir=root / "logs",
            config_dir=root / "config",
        )

    @property
    def database_path(self) -> Path:
        return self.data_dir / "paper-reviewer.db"

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.database_path.as_posix()}"

    @property
    def preferences_path(self) -> Path:
        return self.config_dir / "preferences.json"

    def ensure(self) -> None:
        for path in (self.root, self.data_dir, self.runs_dir, self.logs_dir, self.config_dir):
            path.mkdir(parents=True, exist_ok=True)


class GuiPreferences(BaseModel):
    theme: str = "system"
    motion: str = "system"
    sidebar_expanded: bool = True
    current_navigation: str = "new_review"
    default_provider: str = "openai"
    default_model: str = "gpt-5-mini"
    default_rubric: str = ""
    external_search: bool = True
    recent_models: dict[str, list[str]] = Field(default_factory=dict)
    # The id is persisted so an interrupted desktop session can reopen the
    # latest in-progress run.  It intentionally contains no paper path or
    # inspection-report path.
    active_run_id: str | None = None

    @field_validator("theme", mode="before")
    @classmethod
    def validate_theme(cls, value: object) -> str:
        allowed = {"system", "light", "dark", "high_contrast"}
        return str(value) if value in allowed else "system"


class PreferencesStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> GuiPreferences:
        if not self.path.is_file():
            return GuiPreferences()
        try:
            return GuiPreferences.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return GuiPreferences()

    def save(self, preferences: GuiPreferences) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(preferences.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, self.path)


def read_json_lines(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows
