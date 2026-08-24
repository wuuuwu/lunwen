from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from paper_reviewer.domain.rubric import RubricProfile


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PAPER_REVIEW_",
        extra="ignore",
    )

    database_url: str = "sqlite+aiosqlite:///./paper-reviewer.db"
    runs_dir: Path = Path("runs")
    request_timeout_seconds: float = 120
    external_timeout_seconds: float = 20
    max_model_turns: int = 3
    max_tool_calls: int = 8
    max_output_repairs: int = 2
    reviewer_concurrency: int = 3
    trace_content: bool = False


class ReviewerProfile(BaseModel):
    reviewer_id: str
    title: str
    description: str
    dimension_ids: list[str] = Field(default_factory=list)
    dimension_tags: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    max_model_turns: int = Field(default=3, ge=1)
    max_tool_calls: int = Field(default=8, ge=0)


class ReviewProfile(BaseModel):
    profile_id: str
    version: str
    reviewers: list[ReviewerProfile] = Field(min_length=1)


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"expected a YAML object in {path}")
    return payload


def load_rubric(path: Path) -> RubricProfile:
    return RubricProfile.model_validate(load_yaml(path))


def load_review_profile(path: Path) -> ReviewProfile:
    return ReviewProfile.model_validate(load_yaml(path))


def stable_config_hash(*models: BaseModel) -> str:
    encoded = json.dumps(
        [model.model_dump(mode="json") for model in models],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
