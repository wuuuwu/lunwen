from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from paper_reviewer.domain.rubric import RubricProfile

REPORT_PRESENTATION_FILENAME = "report-presentation.json"


class ReportPresentationProfile(StrEnum):
    LEGACY = "legacy"
    ZH_CN_V1 = "zh_cn_v1"
    COURSE_ZH_CN_V1 = "course_zh_cn_v1"


class ReportPresentationMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1, le=1)
    profile: ReportPresentationProfile
    locale: str = "zh-CN"


_SEVERITY_LABELS = {
    "critical": "严重",
    "major": "主要",
    "minor": "次要",
    "suggestion": "建议",
}

_HARD_RULE_STATUS_LABELS = {
    "not_detected": "未发现",
    "suspected": "疑似存在问题",
    "confirmed": "确认成立",
    "dismissed": "确认不成立",
    "not_assessable": "无法判断",
}

_EXPERT_VERDICT_LABELS = {
    "qualified": "合格",
    "unqualified": "不合格",
    "unable_to_assess": "无法判断",
}

_PANEL_OUTCOME_LABELS = {
    "risk_triggered": "触发存在问题风险",
    "risk_not_triggered": "未触发存在问题风险",
    "awaiting_hard_rule_confirmation": "待否决项人工复核",
    "supplemental_required": "需要追加复评",
    "awaiting_panel_review": "待人工面板复核",
}

_HUMAN_DECISION_LABELS = {
    "confirmed": "确认成立",
    "dismissed": "确认不成立",
    "risk_triggered": "触发风险",
    "risk_not_triggered": "未触发风险",
}

_DECISION_STEP_LABELS = {
    "hard_rule_confirmed": "人工确认否决项成立",
    "hard_rule_human_confirmation_required": "否决项需要人工复核",
    "expert_panel_unable_to_assess": "专家面板无法完成判断",
    "human_panel_risk_triggered": "人工面板确认触发风险",
    "human_panel_risk_not_triggered": "人工面板确认未触发风险",
    "initial_unable_to_assess": "初评专家无法完成判断",
    "human_panel_review_required": "需要人工面板复核",
    "initial_panel_incomplete": "三名初评专家意见尚未完整",
    "initial_unqualified_at_least_two": "三名初评专家中至少两名判定不合格",
    "initial_unqualified_zero": "三名初评专家均判定合格",
    "supplemental_unable_to_assess": "复评专家无法完成判断",
    "initial_unqualified_one": "三名初评专家中恰好一名判定不合格",
    "supplemental_required": "需要追加两名复评专家",
    "supplemental_unqualified_at_least_one": "复评专家中至少一名判定不合格",
    "supplemental_unqualified_zero": "两名复评专家均判定合格",
    "risk_triggered": "触发存在问题风险",
    "risk_not_triggered": "未触发存在问题风险",
}

_PANEL_REASON_LABELS = {
    "A hard rule was confirmed by a human reviewer.": "人工复核确认至少一项否决项成立。",
    "One or more hard rules require a human decision.": "至少一项否决项需要人工复核。",
    "A human panel resolved the AI experts' inability to assess.": (
        "人工面板已对 AI 专家无法判断的情况作出结论。"
    ),
    "An initial expert was unable to assess the paper.": "至少一名初评专家无法完成判断。",
    "Three independent initial opinions are required.": "需要三名独立初评专家的完整意见。",
    "At least two of three initial experts found the paper unqualified.": (
        "三名初评专家中至少两名判定论文不合格。"
    ),
    "All three initial experts found the paper qualified.": "三名初评专家均判定论文合格。",
    "A supplemental expert was unable to assess the paper.": "至少一名复评专家无法完成判断。",
    "Exactly one initial expert voted unqualified; two supplemental opinions are required.": (
        "三名初评专家中恰好一名判定不合格，需要追加两名复评专家。"
    ),
    "At least one supplemental expert found the paper unqualified.": (
        "复评专家中至少一名判定论文不合格。"
    ),
    "Both supplemental experts found the paper qualified.": "两名复评专家均判定论文合格。",
}

_GENERIC_NARRATIVE_ALIASES = {
    "finding_id": "关联问题",
    "dimension_id": "指标",
    "criterion_id": "指标",
    "group_id": "指标分组",
    "rule_id": "规则",
    "reviewer_id": "评阅人",
}

_PROTECTED_TEXT = re.compile(r"(`[^`\r\n]*`|https?://[^\s<>()]+)", re.IGNORECASE)


def load_presentation_profile(run_dir: Path) -> ReportPresentationProfile:
    path = run_dir / REPORT_PRESENTATION_FILENAME
    if not path.is_file():
        return ReportPresentationProfile.LEGACY
    try:
        metadata = ReportPresentationMetadata.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        raise ValueError("报告展示配置损坏，无法安全确定报告格式。") from error
    return metadata.profile


class ReportPresentation:
    """Resolve stable report identifiers into task-facing labels."""

    def __init__(
        self,
        rubric: RubricProfile,
        profile: ReportPresentationProfile = ReportPresentationProfile.LEGACY,
    ) -> None:
        self.rubric = rubric
        self.profile = ReportPresentationProfile(profile)
        self.dimension_titles = {
            item.dimension_id: item.title.strip() or "未命名指标"
            for item in rubric.dimensions
        }
        self.group_titles = {
            item.group_id: item.title.strip() or "未命名分组" for item in rubric.groups
        }
        self.rule_titles = {
            item.rule_id: ((item.title or "").strip() or item.description.strip() or "未命名规则")
            for item in rubric.hard_rules
        }

    @property
    def localized(self) -> bool:
        return self.profile in {
            ReportPresentationProfile.ZH_CN_V1,
            ReportPresentationProfile.COURSE_ZH_CN_V1,
        }

    @property
    def course(self) -> bool:
        return self.profile is ReportPresentationProfile.COURSE_ZH_CN_V1

    def dimension(self, value: Any) -> str:
        raw = _value(value)
        if not self.localized:
            return raw
        return self.dimension_titles.get(raw, "未命名指标")

    def group(self, value: Any) -> str:
        raw = _value(value)
        if not self.localized:
            return raw
        return self.group_titles.get(raw, "未命名分组")

    def rule(self, value: Any) -> str:
        raw = _value(value)
        if not self.localized:
            return raw
        return self.rule_titles.get(raw, "未命名规则")

    def severity(self, value: Any) -> str:
        return self._enum_label(value, _SEVERITY_LABELS)

    def hard_rule_status(self, value: Any) -> str:
        return self._enum_label(value, _HARD_RULE_STATUS_LABELS)

    def expert_verdict(self, value: Any) -> str:
        return self._enum_label(value, _EXPERT_VERDICT_LABELS)

    def panel_outcome(self, value: Any) -> str:
        return self._enum_label(value, _PANEL_OUTCOME_LABELS)

    def human_decision(self, value: Any) -> str:
        return self._enum_label(value, _HUMAN_DECISION_LABELS)

    def decision_step(self, value: Any) -> str:
        return self._enum_label(value, _DECISION_STEP_LABELS)

    def panel_reason(self, value: Any) -> str:
        raw = _value(value)
        if not self.localized:
            return raw
        return _PANEL_REASON_LABELS.get(raw, self.narrative(raw))

    def expert_label(self, round_value: Any, index: int, expert_id: Any = "") -> str:
        if not self.localized:
            return _value(expert_id)
        prefix = "复评专家" if _value(round_value).casefold() == "supplemental" else "初评专家"
        return f"{prefix} {index}"

    def narrative(
        self,
        value: Any,
        *,
        extra_aliases: Mapping[str, str] | None = None,
    ) -> str:
        text = _value(value)
        if not self.localized or not text:
            return text
        aliases = {
            **self.dimension_titles,
            **self.group_titles,
            **self.rule_titles,
            **_SEVERITY_LABELS,
            **_HARD_RULE_STATUS_LABELS,
            **_EXPERT_VERDICT_LABELS,
            **_PANEL_OUTCOME_LABELS,
            **_HUMAN_DECISION_LABELS,
            **_DECISION_STEP_LABELS,
            **_GENERIC_NARRATIVE_ALIASES,
        }
        if extra_aliases:
            aliases.update({str(key): str(label) for key, label in extra_aliases.items()})
        return "".join(
            part if index % 2 else _replace_known_identifiers(part, aliases)
            for index, part in enumerate(_PROTECTED_TEXT.split(text))
        )

    def _enum_label(self, value: Any, labels: Mapping[str, str]) -> str:
        raw = _value(value)
        if not self.localized:
            return raw
        return labels.get(raw.casefold(), raw)


def _replace_known_identifiers(text: str, aliases: Mapping[str, str]) -> str:
    keys = [key for key in aliases if key]
    if not keys:
        return text
    pattern = re.compile(
        r"(?<![A-Za-z0-9_-])(?:"
        + "|".join(re.escape(key) for key in sorted(keys, key=len, reverse=True))
        + r")(?![A-Za-z0-9_-])"
    )
    return pattern.sub(lambda match: aliases[match.group(0)], text)


def _value(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "value", value))
