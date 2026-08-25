from __future__ import annotations

import asyncio
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from paper_reviewer.application.app_state import AppPaths, GuiPreferences
from paper_reviewer.application.models import (
    ProviderCompatibilityResult,
    ProviderErrorDetails,
    ProviderResponseDiagnostics,
)
from paper_reviewer.domain.provider import CustomProviderProfile, ModelApiProtocol
from paper_reviewer.gui.icons import FluentIconService
from paper_reviewer.gui.operations import AsyncOperationRegistry
from paper_reviewer.gui.pages.settings import SettingsPage
from paper_reviewer.gui.provider_widgets import ProviderEditorDialog, ProviderTableModel
from paper_reviewer.gui.theme import FluentThemeManager
from paper_reviewer.gui.worker import AsyncTaskThread


class _Credentials:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def has(self, provider: str) -> bool:
        return provider in self.values

    def set(self, provider: str, value: str) -> None:
        self.values[provider] = value

    def delete(self, provider: str) -> None:
        self.values.pop(provider, None)


class _Service:
    def __init__(self, profile: CustomProviderProfile) -> None:
        self.credentials = _Credentials()
        self.items = [profile]

    def validate_rubric(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("rubric validation is not part of this test")

    def list_custom_providers(self, *, include_archived: bool = False) -> list[object]:
        return [item for item in self.items if include_archived or not item.is_archived]

    def list_provider_connections(self, *, include_archived: bool = False) -> list[object]:
        del include_archived
        return self.items

    def custom_provider_has_key(self, _provider_ref: str) -> bool:
        return False


def _profile() -> CustomProviderProfile:
    return CustomProviderProfile(
        provider_id="a" * 32,
        display_name="校内模型",
        protocol=ModelApiProtocol.RESPONSES,
        base_url="https://model.example.edu/v1",
        default_model="reviewer-v1",
    )


def test_provider_table_model_exposes_protocol_and_key_state() -> None:
    model = ProviderTableModel()
    profile = _profile()
    model.set_items([profile], {profile.provider_ref: True})

    assert model.rowCount() == 1
    assert model.data(model.index(0, 0)) == "校内模型"
    assert model.data(model.index(0, 1)) == "Responses API"
    assert model.data(model.index(0, 4)) == "已配置 Key"
    assert model.provider_ref(0) == profile.provider_ref


def test_provider_editor_validates_url_and_keeps_endpoint_read_only(qapp) -> None:
    profile = _profile()
    dialog = ProviderEditorDialog(existing=profile, has_key=True)
    assert dialog.protocol.isEnabled() is False
    assert dialog.base_url.isReadOnly() is True

    dialog.base_url.setText("http://remote.example/v1")
    assert dialog.validate_fields() is not None
    # Existing endpoint is immutable; changing the visible text is ignored by
    # the normal save path because it is not validated as a replacement.
    dialog._enable_replacement()
    assert dialog.protocol.isEnabled() is True
    assert dialog.base_url.isReadOnly() is False
    assert dialog.validate_fields(require_api_key=True) is None
    assert dialog.base_url.property("fluentInvalid") is True
    dialog.reject()


def test_settings_page_lists_custom_provider_and_escape_closes_editor(qapp, tmp_path: Path) -> None:
    profile = _profile()
    service = _Service(profile)
    paths = AppPaths(
        root=tmp_path,
        data_dir=tmp_path / "data",
        runs_dir=tmp_path / "runs",
        logs_dir=tmp_path / "logs",
        config_dir=tmp_path / "config",
    )
    theme = FluentThemeManager(qapp)
    page = SettingsPage(service, GuiPreferences(), paths, FluentIconService(theme))
    assert page.provider_table_model.rowCount() == 1
    assert page.provider_table_model.data(page.provider_table_model.index(0, 0)) == "校内模型"
    provider_refs = [page.provider.itemData(index) for index in range(page.provider.count())]
    assert provider_refs[:3] == ["openai", "openai_responses", "deepseek"]
    assert profile.provider_ref in provider_refs
    responses_index = page.provider.findData("openai_responses")
    assert "Responses API" in page.provider.itemText(responses_index)

    custom_index = page.provider.findData(profile.provider_ref)
    page.provider.setCurrentIndex(custom_index)
    page._default_provider_activated(custom_index)
    assert page.model.text() == "reviewer-v1"

    dialog = ProviderEditorDialog(existing=profile, parent=page)
    dialog.show()
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    assert not dialog.isVisible()
    page.deleteLater()


def test_provider_compatibility_error_displays_only_sanitized_fields(qapp, tmp_path: Path) -> None:
    profile = _profile()
    service = _Service(profile)
    paths = AppPaths(
        root=tmp_path,
        data_dir=tmp_path / "data",
        runs_dir=tmp_path / "runs",
        logs_dir=tmp_path / "logs",
        config_dir=tmp_path / "config",
    )
    theme = FluentThemeManager(qapp)
    page = SettingsPage(service, GuiPreferences(), paths, FluentIconService(theme))
    dialog = ProviderEditorDialog(parent=page)
    page._provider_dialog = dialog

    page._provider_test_completed(
        ProviderCompatibilityResult(
            compatible=False,
            message="BadRequestError: Provider 拒绝了请求。(HTTP 400)",
            protocol=ModelApiProtocol.CHAT_COMPLETIONS,
            error_details=ProviderErrorDetails(
                message="tools are not supported",
                code="unsupported_parameter",
                param="tool_choice",
            ),
            response_diagnostics=ProviderResponseDiagnostics(
                response_status="incomplete",
                incomplete_reason="max_output_tokens",
                finish_reason="length",
                output_item_types=["reasoning", "message"],
                plain_text_only=True,
            ),
        )
    )

    assert dialog.error_label.textFormat() is Qt.TextFormat.PlainText
    assert dialog.error_label.text() == (
        "兼容性测试失败：BadRequestError: Provider 拒绝了请求。(HTTP 400)\n"
        "服务商返回（已脱敏）：\n"
        "message: tools are not supported\n"
        "code: unsupported_parameter\n"
        "param: tool_choice\n"
        "响应诊断（不含正文）：\n"
        "response.status: incomplete\n"
        "incomplete_details.reason: max_output_tokens\n"
        "finish_reason: length\n"
        "output item 类型: reasoning、message\n"
        "仅返回普通文本: 是"
    )
    assert dialog.error_label.accessibleName() == "Provider 兼容性测试结果"
    assert dialog.error_label.accessibleDescription() == dialog.error_label.text()
    dialog.reject()
    page.deleteLater()


def test_settings_provider_worker_uses_window_registry_for_cancel_and_cleanup(
    qapp, qtbot, tmp_path: Path
) -> None:
    profile = _profile()
    service = _Service(profile)
    paths = AppPaths(
        root=tmp_path,
        data_dir=tmp_path / "data",
        runs_dir=tmp_path / "runs",
        logs_dir=tmp_path / "logs",
        config_dir=tmp_path / "config",
    )
    theme = FluentThemeManager(qapp)
    registry = AsyncOperationRegistry()
    page = SettingsPage(
        service,
        GuiPreferences(),
        paths,
        FluentIconService(theme),
        operation_registry=registry,
    )

    async def operation(_emit: object) -> None:
        await asyncio.Event().wait()

    worker = AsyncTaskThread(operation)
    page._register_provider_worker(worker)
    assert page._provider_test_workers == [worker]
    assert registry.workers == [worker]
    worker.start()
    qtbot.waitUntil(worker.isRunning, timeout=3000)

    with qtbot.waitSignal(worker.task_cancelled, timeout=3000):
        cancelled = registry.cancel_running()
        assert cancelled == [worker]
    assert worker.wait(3000)
    qapp.processEvents()
    assert page._provider_test_workers == []
    assert registry.workers == []
    page.deleteLater()
