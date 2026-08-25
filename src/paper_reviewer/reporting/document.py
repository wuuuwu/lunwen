"""Read-only projections used by report renderers.

The application stores both the legacy ``MetaReview`` shape and the newer
``EvaluationReport`` shape.  Report consumers should not need to know which
shape was loaded from a task directory.  ``ReportDocument`` is deliberately a
small, frozen projection: it carries the report payload and the rendering
context, while leaving the persisted domain models unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from paper_reviewer.domain.provider import ModelApiProtocol, ProviderSnapshot
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.reporting.presentation import ReportPresentation, ReportPresentationProfile
from paper_reviewer.validation.audits import AuditReport


class ReportKind(StrEnum):
    """Stable kind discriminator for report projection consumers."""

    LEGACY = "legacy"
    EVALUATION = "evaluation"


@dataclass(frozen=True, slots=True)
class ReportDocument:
    """A frozen, task-facing report projection.

    ``report`` intentionally remains typed as ``Any`` because v1 snapshots are
    heterogeneous and must stay readable.  The projection itself is immutable
    and does not expose or alter the serialized artifact; adapters only retain
    the already-loaded model reference for deterministic rendering.
    """

    rubric: RubricProfile
    report: Any
    audit: AuditReport
    kind: ReportKind
    presentation_profile: ReportPresentationProfile
    provider_snapshot: ProviderSnapshot | None = None
    provider_ref: str | None = None
    model: str | None = None

    @property
    def is_evaluation(self) -> bool:
        return self.kind is ReportKind.EVALUATION

    @property
    def presentation(self) -> ReportPresentation:
        """Resolve display labels from the captured rubric snapshot."""

        return ReportPresentation(self.rubric, self.presentation_profile)

    @property
    def provider_lines(self) -> tuple[str, ...]:
        """Return safe provider identity lines for Markdown and PDF output."""

        selected_model: str | None
        if self.provider_snapshot is not None:
            display_name = self.provider_snapshot.display_name
            protocol = self.provider_snapshot.protocol
            selected_model = self.provider_snapshot.model
        else:
            builtins = {
                "openai": ("OpenAI", ModelApiProtocol.CHAT_COMPLETIONS),
                "openai_responses": ("OpenAI", ModelApiProtocol.RESPONSES),
                "deepseek": ("DeepSeek", ModelApiProtocol.CHAT_COMPLETIONS),
            }
            display_name, protocol = builtins.get(
                self.provider_ref or "",
                ("自定义 Provider", ModelApiProtocol.CHAT_COMPLETIONS),
            )
            selected_model = self.model
        protocol_name = (
            "Responses API"
            if protocol is ModelApiProtocol.RESPONSES
            else "Chat Completions"
        )
        lines = [
            f"- Provider：{display_name}",
            f"- 接口协议：{protocol_name}",
        ]
        if selected_model:
            lines.append(f"- 模型：{selected_model}")
        return tuple(lines)
