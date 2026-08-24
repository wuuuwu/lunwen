from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from paper_reviewer.application.models import RubricValidationResult
from paper_reviewer.gui.icons import FluentIconService
from paper_reviewer.gui.theme import set_fluent_property


class PageHeader(QWidget):
    def __init__(self, title: str, subtitle: str) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 8)
        layout.setSpacing(4)
        title_label = QLabel(title)
        title_label.setProperty("fluentType", "pageTitle")
        subtitle_label = QLabel(subtitle)
        subtitle_label.setProperty("fluentType", "secondary")
        subtitle_label.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(subtitle_label)


class MessageBar(QFrame):
    action_requested = Signal()

    def __init__(self, icons: FluentIconService) -> None:
        super().__init__()
        self.icons = icons
        self.setProperty("fluentRole", "messageBar")
        self.setProperty("fluentSeverity", "info")
        self._icon_name = "info"
        self._icon_color_role = "info_foreground"
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(20, 20)
        self.message_label = QLabel()
        self.message_label.setWordWrap(True)
        self.message_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.action_button = QPushButton()
        set_fluent_property(self.action_button, "fluentAppearance", "subtle")
        self.action_button.clicked.connect(self.action_requested)
        self.action_button.hide()
        layout.addWidget(self.icon_label)
        layout.addWidget(self.message_label, 1)
        layout.addWidget(self.action_button)
        icons.theme.theme_changed.connect(self._refresh_icon)
        self.hide()

    def show_message(
        self, message: str, *, severity: str = "info", action_text: str | None = None
    ) -> None:
        set_fluent_property(self, "fluentSeverity", severity)
        self._icon_name = {"success": "check", "warning": "warning", "danger": "error"}.get(
            severity, "info"
        )
        self._icon_color_role = {
            "success": "success_foreground",
            "warning": "warning_foreground",
            "danger": "danger_foreground",
        }.get(severity, "info_foreground")
        self._refresh_icon()
        self.message_label.setText(message)
        self.setAccessibleName(message)
        if action_text:
            self.action_button.setText(action_text)
            self.action_button.setAccessibleName(action_text)
            self.action_button.show()
        else:
            self.action_button.hide()
        self.show()

    def clear(self) -> None:
        self.hide()
        self.message_label.clear()

    def _refresh_icon(self, _mode: str = "") -> None:
        icon = self.icons.icon(self._icon_name, color_role=self._icon_color_role)
        self.icon_label.setPixmap(icon.pixmap(20, 20))


class DropPathEdit(QLineEdit):
    path_dropped = Signal(str)

    def __init__(self, *, suffix: str) -> None:
        super().__init__()
        self.suffix = suffix.lower()
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: object) -> None:
        from PySide6.QtGui import QDragEnterEvent

        if not isinstance(event, QDragEnterEvent):
            return
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].toLocalFile().lower().endswith(self.suffix):
            event.acceptProposedAction()

    def dropEvent(self, event: object) -> None:
        from PySide6.QtGui import QDropEvent

        if not isinstance(event, QDropEvent):
            return
        urls = event.mimeData().urls()
        if len(urls) != 1:
            return
        path = urls[0].toLocalFile()
        unchanged = self.text() == path
        self.setText(path)
        if unchanged:
            self.path_dropped.emit(path)
        event.acceptProposedAction()


class PathPicker(QWidget):
    path_changed = Signal(str)
    browse_requested = Signal()

    def __init__(self, *, suffix: str, placeholder: str, button_text: str = "浏览…") -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.edit = DropPathEdit(suffix=suffix)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setAccessibleName(placeholder)
        self.button = QPushButton(button_text)
        self.button.clicked.connect(self.browse_requested)
        self.edit.textChanged.connect(self.path_changed)
        self.edit.path_dropped.connect(self.path_changed)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button)

    def path(self) -> Path | None:
        text = self.edit.text().strip()
        return Path(text) if text else None

    def set_path(self, path: Path | str) -> None:
        self.edit.setText(str(path))

    def set_invalid(self, message: str | None) -> None:
        set_fluent_property(self.edit, "fluentInvalid", bool(message))
        self.edit.setAccessibleDescription(message or "")
        self.edit.setToolTip(message or "")


class RubricPreview(QFrame):
    """Render the currently validated rubric without knowing its dimensions.

    Rubrics are user-owned configuration, so the preview must not make
    assumptions about a particular set of criteria.  Version 1 rubrics have a
    flat ``dimensions`` list; the Zhejiang v2 rubric adds first-level groups,
    a policy context and an independent-panel policy.  The small helpers below
    intentionally read both Pydantic models and mapping-like values.  This
    keeps the widget compatible with a saved v1 snapshot while allowing the
    domain schema to evolve without making the GUI a second schema validator.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setProperty("fluentRole", "card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)
        self.title = QLabel("尚未选择 Rubric")
        self.title.setProperty("fluentType", "sectionTitle")
        self.metadata = QLabel("选择 YAML 后将显示结构、权重和评分状态。")
        self.metadata.setProperty("fluentType", "secondary")
        self.metadata.setWordWrap(True)
        self.details = QLabel()
        self.details.setProperty("fluentType", "secondary")
        self.details.setWordWrap(True)
        self.tree = QTreeView()
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setMinimumHeight(180)
        self.tree.setAccessibleName("Rubric 结构预览")
        self.tree.setAccessibleDescription("按一级指标、二级指标和评分锚点展示 Rubric 内容")
        self.model = QStandardItemModel()
        self.tree.setModel(self.model)
        layout.addWidget(self.title)
        layout.addWidget(self.metadata)
        layout.addWidget(self.details)
        layout.addWidget(self.tree, 1)

    def set_result(self, result: RubricValidationResult) -> None:
        self.model.clear()
        rubric = result.rubric
        if rubric is None:
            self.title.setText("Rubric 校验失败")
            self.metadata.setText("；".join(result.errors))
            self.details.clear()
            return

        mode = "启用评分" if _bool_value(_value(rubric, "scoring_enabled")) else "仅评语"
        self.title.setText(
            _string_value(_value(rubric, "title", _value(rubric, "name")), "未命名 Rubric")
        )
        levels = "、".join(
            _string_value(item) for item in _sequence_value(_value(rubric, "applicable_levels"))
        ) or "未声明"
        schema_version = _string_value(_value(rubric, "schema_version"), "1")
        dimensions = _sequence_value(
            _value(rubric, "dimensions", _value(rubric, "criteria"))
        )
        weight_total = _number_value(_value(result, "weight_total"), 0)
        if not weight_total:
            weight_total = sum(
                _number_value(_value(dimension, "weight"), 0) for dimension in dimensions
            )
        metadata = (
            f"{_string_value(_value(rubric, 'rubric_id'))}@"
            f"{_string_value(_value(rubric, 'version'))} · Schema {schema_version} · "
            f"{mode} · 适用层级：{levels} · 权重合计：{weight_total:g}%"
        )
        metadata_suffix = self._metadata_suffix(rubric, result)
        if metadata_suffix:
            metadata += " · " + " · ".join(metadata_suffix)
        self.metadata.setText(metadata)

        self.details.setText(self._details_text(rubric, result))
        grouped = self._grouped_dimensions(rubric, dimensions)
        if grouped:
            criteria_root = QStandardItem(f"一级指标分组（{len(grouped)}）")
            self._make_read_only(criteria_root)
            for group, group_dimensions in grouped:
                group_item = QStandardItem(self._group_label(group, group_dimensions))
                self._make_read_only(group_item)
                for dimension in group_dimensions:
                    group_item.appendRow(self._dimension_item(rubric, dimension))
                criteria_root.appendRow(group_item)
        else:
            # Preserve the v1 tree shape.  Existing saved rubrics and users
            # accustomed to this layout should not be forced through a v2
            # grouping model merely because the preview widget was upgraded.
            criteria_root = QStandardItem(f"评分维度（{len(dimensions)}）")
            self._make_read_only(criteria_root)
            for dimension in dimensions:
                criteria_root.appendRow(self._dimension_item(rubric, dimension))

        self.model.appendRow(criteria_root)
        self._append_rating_scale(rubric)

        hard_rules = self._hard_rules(rubric)
        hard_root = QStandardItem(
            f"结构化否决项（硬性规则，{len(hard_rules)}，须人工确认）"
            if hard_rules
            else "结构化否决项（硬性规则，0）"
        )
        self._make_read_only(hard_root)
        for rule in hard_rules:
            hard_root.appendRow(self._hard_rule_item(rule))
        self.model.appendRow(hard_root)

        panel = self._panel_policy(rubric)
        if panel:
            panel_root = QStandardItem("独立专家面板")
            self._make_read_only(panel_root)
            for line in panel:
                item = QStandardItem(line)
                self._make_read_only(item)
                panel_root.appendRow(item)
            self.model.appendRow(panel_root)

        self.tree.expandAll()

    @staticmethod
    def _make_read_only(item: QStandardItem) -> None:
        item.setEditable(False)

    @staticmethod
    def _grouped_dimensions(
        rubric: object, dimensions: list[object]
    ) -> list[tuple[object, list[object]]]:
        """Return explicit v2 groups, or groups inferred from dimension IDs.

        No group is inferred for a legacy flat rubric.  This is important for
        v1 compatibility: a single synthetic group would change the model
        shape and make old automation and keyboard navigation surprising.
        """

        by_id = {
            _string_value(_value(dimension, "dimension_id")): dimension
            for dimension in dimensions
            if _string_value(_value(dimension, "dimension_id"))
        }
        raw_groups = _sequence_value(
            _value(
                rubric,
                "groups",
                _value(
                    rubric,
                    "criterion_groups",
                    _value(rubric, "indicator_groups"),
                ),
            )
        )
        grouped: list[tuple[object, list[object]]] = []
        if raw_groups:
            remaining = list(dimensions)
            for group in raw_groups:
                group_items = _sequence_value(
                    _value(
                        group,
                        "dimensions",
                        _value(
                            group,
                            "criteria",
                            _value(group, "dimension_ids", _value(group, "criterion_ids")),
                        ),
                    )
                )
                resolved: list[object] = []
                for item in group_items:
                    item_id = _string_value(_value(item, "dimension_id", item))
                    resolved.append(by_id.get(item_id, item))
                if not resolved:
                    group_id = _string_value(
                        _value(group, "group_id", _value(group, "id"))
                    )
                    resolved = [
                        dimension
                        for dimension in remaining
                        if _string_value(
                            _value(
                                dimension,
                                "group_id",
                                _value(dimension, "criterion_group_id"),
                            )
                        )
                        == group_id
                    ]
                resolved = [item for item in resolved if item in dimensions]
                remaining = [item for item in remaining if item not in resolved]
                grouped.append((group, resolved))
            if remaining:
                grouped.append(({"title": "其他指标"}, remaining))
            return grouped

        by_group: dict[str, list[object]] = {}
        for dimension in dimensions:
            group_id = _string_value(
                _value(
                    dimension,
                    "group_id",
                    _value(dimension, "criterion_group_id", _value(dimension, "category")),
                )
            )
            if group_id:
                by_group.setdefault(group_id, []).append(dimension)
        return [(group_id, items) for group_id, items in by_group.items()]

    @staticmethod
    def _group_label(group: object, dimensions: list[object]) -> str:
        if isinstance(group, str):
            title = group
            description = ""
        else:
            title = _string_value(
                _value(group, "title", _value(group, "name", _value(group, "group_id"))),
                "未命名一级指标",
            )
            description = _string_value(_value(group, "description"))
        weight = _value(group, "weight")
        suffix = f" · 权重 {_number_value(weight):g}%" if weight is not None else ""
        if description:
            suffix += f" · {description}"
        return f"{title}（{len(dimensions)}项）{suffix}"

    def _dimension_item(self, rubric: object, dimension: object) -> QStandardItem:
        title = _string_value(_value(dimension, "title"), "未命名指标")
        weight = _number_value(_value(dimension, "weight"))
        minimum = _number_value(_value(dimension, "minimum_score"), 0)
        maximum = _number_value(_value(dimension, "maximum_score"), 4)
        if _schema_is_v2(rubric):
            # The policy's diagnostic scale is deliberately integer 0–4.  Use
            # the configured range when a custom compatible rubric provides it.
            minimum = _number_value(_value(dimension, "minimum_score"), 0)
            maximum = _number_value(_value(dimension, "maximum_score"), 4)
        text = f"{title} · 权重 {weight:g}% · {minimum:g}–{maximum:g}"
        tags = _sequence_value(_value(dimension, "reviewer_tags"))
        if tags:
            text += f" · Reviewer：{'、'.join(_string_value(tag) for tag in tags)}"
        parent = QStandardItem(text)
        self._make_read_only(parent)

        description = _string_value(_value(dimension, "description"))
        if description:
            item = QStandardItem(f"要求：{description}")
            self._make_read_only(item)
            parent.appendRow(item)

        checks = _sequence_value(_value(dimension, "checks"))
        if checks:
            check_root = QStandardItem("检查点：" + "；".join(_string_value(c) for c in checks))
            self._make_read_only(check_root)
            parent.appendRow(check_root)

        anchors = _sequence_value(_value(dimension, "anchors"))
        if anchors:
            self._append_anchors(parent, anchors)
        elif not anchors:
            global_anchors = _global_anchors(rubric)
            if global_anchors:
                self._append_anchors(parent, global_anchors)
        policy = _value(dimension, "evidence_policy")
        if policy:
            policy_item = QStandardItem(
                "证据要求：" + _evidence_policy_text(policy)
            )
            self._make_read_only(policy_item)
            parent.appendRow(policy_item)
        return parent

    def _append_anchors(self, parent: QStandardItem, anchors: list[object]) -> None:
        anchor_root = QStandardItem(f"评分锚点（{len(anchors)}级）")
        self._make_read_only(anchor_root)
        for anchor in anchors:
            label = _string_value(
                _value(anchor, "label", _value(anchor, "score", _value(anchor, "rating"))),
                "未命名",
            )
            minimum = _value(anchor, "minimum", _value(anchor, "min"))
            maximum = _value(anchor, "maximum", _value(anchor, "max"))
            range_text = ""
            if minimum is not None and maximum is not None:
                range_text = f" · {_number_value(minimum):g}–{_number_value(maximum):g}"
            description = _string_value(
                _value(anchor, "description", _value(anchor, "text")),
            )
            item = QStandardItem(f"{label}{range_text} · {description}".rstrip(" ·"))
            self._make_read_only(item)
            anchor_root.appendRow(item)
        parent.appendRow(anchor_root)

    def _append_rating_scale(self, rubric: object) -> None:
        anchors = _global_anchors(rubric)
        if not anchors:
            return
        item = QStandardItem(f"统一评分刻度（{len(anchors)}级，0–4）")
        self._make_read_only(item)
        for anchor in anchors:
            label = _string_value(
                _value(anchor, "label", _value(anchor, "score", _value(anchor, "rating"))),
                "未命名",
            )
            description = _string_value(
                _value(anchor, "description", _value(anchor, "text")),
            )
            child = QStandardItem(f"{label} · {description}".rstrip(" ·"))
            self._make_read_only(child)
            item.appendRow(child)
        self.model.appendRow(item)

    @staticmethod
    def _hard_rules(rubric: object) -> list[object]:
        rules = _value(
            rubric,
            "hard_rules",
            _value(rubric, "hard_rule_policy"),
        )
        if isinstance(rules, Mapping):
            nested = rules.get("rules")
            if nested is not None:
                rules = nested
        return _sequence_value(rules)

    def _hard_rule_item(self, rule: object) -> QStandardItem:
        rule_id = _string_value(_value(rule, "rule_id", _value(rule, "id")), "未命名规则")
        description = _string_value(_value(rule, "description"), "未提供规则描述")
        outcome = _string_value(_value(rule, "outcome", _value(rule, "risk_outcome")))
        requires_human = _value(
            rule,
            "requires_human_confirmation",
            _value(rule, "human_confirmation_required", True),
        )
        confirmation_text = "是" if _bool_value(requires_human, True) else "否"
        text = f"{rule_id}：{description}"
        if outcome:
            text += f" → {outcome}"
        text += f" · 人工确认：{confirmation_text}"
        item = QStandardItem(text)
        self._make_read_only(item)
        if _bool_value(_value(rule, "evidence_required"), True):
            evidence = QStandardItem("证据要求：必须引用论文证据，AI 只能提出嫌疑")
            self._make_read_only(evidence)
            item.appendRow(evidence)
        return item

    @staticmethod
    def _panel_policy(rubric: object) -> list[str]:
        raw = _value(
            rubric,
            "panel_strategy",
            _value(rubric, "panel", _value(rubric, "expert_panel_policy")),
        )
        if raw is None:
            return []
        initial = _number_value(
            _value(raw, "initial_reviewers", _value(raw, "initial_count")), 3
        )
        supplemental = _number_value(
            _value(raw, "supplemental_reviewers", _value(raw, "supplemental_count")), 2
        )
        lines = [f"策略：{initial:g}+{supplemental:g} 名相互隔离的完整评阅专家"]
        initial_trigger = _string_value(
            _value(raw, "supplemental_trigger", _value(raw, "trigger")),
            "首轮恰 1 人不合格时追加复评",
        )
        lines.append(f"追加条件：{initial_trigger}")
        unable = _string_value(_value(raw, "unable_to_assess"))
        if unable:
            lines.append(f"无法判断：{unable}")
        return lines

    @staticmethod
    def _details_text(rubric: object, result: RubricValidationResult) -> str:
        parts: list[str] = []
        policy = _value(
            rubric,
            "policy_context",
            _value(rubric, "policy", _value(rubric, "policy_source")),
        )
        if policy is not None:
            source = _string_value(
                _value(policy, "source", _value(policy, "title", policy))
            )
            document_number = _string_value(
                _value(policy, "document_number", _value(policy, "document_no"))
            )
            effective_date = _string_value(_value(policy, "effective_date"))
            digest = _string_value(
                _value(policy, "source_sha256", _value(policy, "sha256"))
            )
            policy_text = "政策来源：" + source
            if document_number:
                policy_text += f" · 文号 {document_number}"
            if effective_date:
                policy_text += f" · 生效 {effective_date}"
            if digest:
                policy_text += f" · SHA-256 {digest}"
            parts.append(policy_text)

        experimental = _value(rubric, "experimental", None)
        version = _string_value(_value(rubric, "version"))
        if experimental is True or "experimental" in version.casefold():
            parts.append("实验性 Rubric：未完成教育测量效度验证。")
        evaluation_mode = _string_value(_value(rubric, "evaluation_mode"))
        if evaluation_mode:
            parts.append(f"评测模式：{evaluation_mode}")
        aggregation = _value(rubric, "aggregation")
        method = _string_value(_value(aggregation, "method"))
        passing_score = _value(aggregation, "passing_score")
        if method:
            passing_text = "无及格线" if passing_score is None else f"及格线 {passing_score}"
            parts.append(f"聚合：{method} · {passing_text}")

        compatible = _value(result, "profile_compatible", False)
        coverage = _value(
            result,
            "profile_coverage",
            _value(result, "reviewer_profile_coverage"),
        )
        if coverage is not None:
            parts.append(f"Reviewer Profile 覆盖：{_coverage_text(coverage)}")
        else:
            parts.append(
                "Reviewer Profile 覆盖：完整"
                if _bool_value(compatible)
                else "Reviewer Profile 覆盖：未通过"
            )
        warnings = _sequence_value(_value(result, "warnings"))
        if warnings:
            parts.append("提示：" + "；".join(_string_value(item) for item in warnings))
        return "\n".join(parts)

    @staticmethod
    def _metadata_suffix(rubric: object, result: RubricValidationResult) -> list[str]:
        suffix: list[str] = []
        policy = _value(
            rubric,
            "policy_context",
            _value(rubric, "policy", _value(rubric, "policy_source")),
        )
        if policy is not None:
            source = _string_value(
                _value(policy, "source", _value(policy, "title", policy))
            )
            if source:
                suffix.append(f"政策：{source}")
        version = _string_value(_value(rubric, "version"))
        if _value(rubric, "experimental") is True or "experimental" in version.casefold():
            suffix.append("实验性")
        compatible = _value(result, "profile_compatible", False)
        coverage = _value(
            result,
            "profile_coverage",
            _value(result, "reviewer_profile_coverage"),
        )
        suffix.append(
            "Reviewer Profile 覆盖："
            + _coverage_text(coverage if coverage is not None else compatible)
        )
        return suffix


def _value(container: object, name: str, default: object = None) -> object:
    """Read a field from a Pydantic model or a plain mapping."""

    if isinstance(container, Mapping):
        return container.get(name, default)
    return getattr(container, name, default)


def _string_value(value: object, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _number_value(value: object, default: float = 0) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(_string_value(value))
    except (TypeError, ValueError):
        return default


def _bool_value(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.casefold() in {"true", "1", "yes", "y", "是"}
    return bool(value)


def _sequence_value(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        # Mapping-valued scales are commonly written as ``0: ...``.  Preserve
        # the score key so it can be displayed by _append_anchors.
        return [{"label": key, "description": item} for key, item in value.items()]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _schema_is_v2(rubric: object) -> bool:
    schema = _string_value(_value(rubric, "schema_version"), "1")
    return schema in {"2", "2.0", "v2"} or schema.startswith("2.")


def _global_anchors(rubric: object) -> list[object]:
    scale = _value(
        rubric,
        "rating_scale",
        _value(rubric, "score_anchors", _value(rubric, "anchors")),
    )
    if scale is not None and not isinstance(scale, (str, bytes)):
        nested = _value(scale, "anchors")
        if nested is not None:
            scale = nested
    return _sequence_value(scale)


def _evidence_policy_text(policy: object) -> str:
    paper = _bool_value(_value(policy, "paper_evidence_required"), True)
    external = _bool_value(_value(policy, "external_evidence_required"), False)
    minimum = _number_value(_value(policy, "minimum_references"), 0)
    required: list[str] = []
    if paper:
        required.append("论文证据")
    if external:
        required.append("外部证据")
    if minimum:
        required.append(f"至少 {minimum:g} 条引用")
    return "、".join(required) if required else "按 Rubric 配置"


def _coverage_text(value: object) -> str:
    if isinstance(value, bool):
        return "完整" if value else "未通过"
    if isinstance(value, Mapping):
        covered = value.get("covered", value.get("covered_dimensions"))
        total = value.get("total", value.get("dimension_count"))
        missing = value.get("missing", value.get("missing_dimensions"))
        if covered is not None and total is not None:
            text = f"{covered}/{total}"
        else:
            text = _string_value(value)
        missing_values = _sequence_value(missing)
        if missing_values:
            text += "，缺少：" + "、".join(_string_value(item) for item in missing_values)
        return text
    return _string_value(value)
