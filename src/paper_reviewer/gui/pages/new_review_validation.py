"""Pure form helpers for the new-review page.

The page owns Qt widgets and user interaction.  This module owns the small,
deterministic pieces of form policy so they can be tested without creating a
window and so the widget code remains focused on presentation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from paper_reviewer.application.models import ReviewRequest
from paper_reviewer.gui.models import ProviderDisplay

YAML_SUFFIXES = frozenset({".yaml", ".yml"})


@dataclass(frozen=True)
class StartState:
    """Derived state used to drive the start button and its explanation."""

    configuration_ready: bool
    valid: bool


def validate_discipline_profile(path: Path | None) -> str | None:
    """Return a user-facing error for an optional profile, or ``None``."""

    if path is None:
        return None
    if not path.is_file() or path.suffix.lower() not in YAML_SUFFIXES:
        return "专业培养目标 YAML 不存在或格式不正确"
    return None


def is_valid_pdf(path: Path | None) -> bool:
    """Return whether *path* points to an existing PDF file."""

    return bool(path and path.is_file() and path.suffix.lower() == ".pdf")


def paper_info_text(path: Path | None) -> str:
    """Format the compact paper summary shown beneath the file picker."""

    if path is None:
        return "尚未选择文件"
    if not is_valid_pdf(path):
        return "文件不可用"
    try:
        size_mb = path.stat().st_size / 1024 / 1024
    except OSError:
        return "文件不可用"
    return f"{path.name} · {size_mb:.2f} MB"


def resolve_profile_for_rubric(
    path: Path,
    *,
    zhejiang_profile_path: Path | None,
    legacy_profile_path: Path,
) -> Path:
    """Select the compatible bundled reviewer profile for a Rubric.

    Zhejiang v2 Rubrics use the specialist profile.  Legacy/v1 Rubrics keep
    the original three-reviewer profile for backward compatibility.  Reading
    the schema marker is intentionally conservative: malformed or unreadable
    custom files remain on the legacy path and are rejected by validation.
    """

    if zhejiang_profile_path is None:
        return legacy_profile_path
    if path.name == "zhejiang_undergraduate_thesis_v2.yaml":
        return zhejiang_profile_path
    try:
        schema_line = next(
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("schema_version:")
        )
    except (OSError, StopIteration):
        return legacy_profile_path
    schema_version = schema_line.partition(":")[2].strip().strip("\"'")
    return zhejiang_profile_path if schema_version == "2" else legacy_profile_path


def model_choices(
    provider: ProviderDisplay,
    *,
    recent_models: list[str],
    default_provider: str,
    default_model: str,
    provider_ref: str,
) -> tuple[list[str], str]:
    """Return de-duplicated model choices and the preferred current value."""

    defaults = [provider.default_model] if provider.default_model else []
    if not defaults:
        defaults = ["gpt-5-mini", "gpt-4.1-mini"] if provider_ref in {
            "openai",
            "openai_responses",
        } else ["deepseek-chat"]
    choices = list(dict.fromkeys([*recent_models, *defaults]))
    current = default_model if provider_ref == default_provider else ""
    return choices, current or defaults[0]


def evaluate_start_state(
    *,
    discipline_name: str,
    paper: Path | None,
    rubric_valid: bool,
    discipline_profile_valid: bool,
    model_name: str,
    provider_ref: str,
    provider_key_available: bool,
    cloud_authorized: bool,
    non_classified: bool,
    busy: bool,
) -> StartState:
    """Derive the start-button state without depending on Qt controls."""

    configuration_ready = bool(
        discipline_name.strip()
        and is_valid_pdf(paper)
        and rubric_valid
        and discipline_profile_valid
        and model_name.strip()
    )
    valid = bool(
        configuration_ready
        and provider_ref
        and provider_key_available
        and cloud_authorized
        and non_classified
        and not busy
    )
    return StartState(configuration_ready=configuration_ready, valid=valid)


def build_review_request(
    *,
    paper: Path,
    provider: str,
    model: str,
    rubric: Path,
    profile: Path,
    discipline_name: str,
    discipline_profile: Path | None,
    external_search: bool,
    cloud_processing_authorized: bool,
    contains_classified_material: bool,
) -> ReviewRequest:
    """Build the application request from a validated form snapshot."""

    return ReviewRequest(
        paper=paper,
        provider=provider,
        model=model,
        rubric=rubric,
        profile=profile,
        external_search=external_search,
        discipline_name=discipline_name.strip(),
        discipline_profile=discipline_profile,
        cloud_processing_authorized=cloud_processing_authorized,
        contains_classified_material=contains_classified_material,
    )
