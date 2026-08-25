"""Internal sections for :mod:`settings`.

The settings page intentionally keeps its widget construction in one place so
that the visual layout and the Qt object graph stay stable.  The operational
parts are split into these small mixins instead.  They are not public API;
their method names remain the same as the historical ``SettingsPage``
methods, which is important for signal connections and GUI automation tests.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from paper_reviewer.gui.models import provider_connections, provider_protocol_text
from paper_reviewer.gui.operations import AsyncOperationRegistry
from paper_reviewer.gui.provider_widgets import ProviderEditorDialog, ProviderFormValues
from paper_reviewer.gui.worker import AsyncTaskThread


class SettingsPresenter:
    """Pure, defensive presentation helpers used by the settings sections."""

    @staticmethod
    def provider_ref(profile: object) -> str:
        ref = getattr(profile, "provider_ref", None)
        if callable(ref):
            ref = ref()
        if ref:
            return str(ref)
        if isinstance(profile, dict):
            ref = profile.get("provider_ref")
            if ref:
                return str(ref)
            return "custom:" + str(profile.get("provider_id", ""))
        return "custom:" + str(getattr(profile, "provider_id", ""))

    @staticmethod
    def is_archived(profile: object) -> bool:
        value = getattr(profile, "is_archived", None)
        if value is not None:
            return bool(value() if callable(value) else value)
        if isinstance(profile, dict):
            return profile.get("archived_at") is not None
        return getattr(profile, "archived_at", None) is not None

    @staticmethod
    def safe_error(error: BaseException) -> str:
        text = str(error).replace("\r", " ").replace("\n", " ")
        return text[:500] or "未知错误"

    @staticmethod
    def provider_error_fields(details: object) -> list[str]:
        if details is None:
            return []
        fields: list[str] = []
        for name in ("message", "code", "param"):
            value = details.get(name) if isinstance(details, dict) else getattr(details, name, None)
            if isinstance(value, str) and value:
                fields.append(f"{name}: {value}")
        return fields

    @staticmethod
    def provider_response_fields(diagnostics: object) -> list[str]:
        if diagnostics is None:
            return []

        def value(name: str) -> object:
            if isinstance(diagnostics, dict):
                return diagnostics.get(name)
            return getattr(diagnostics, name, None)

        response_status = value("response_status")
        incomplete_reason = value("incomplete_reason")
        finish_reason = value("finish_reason")
        output_item_types = value("output_item_types")
        plain_text_only = value("plain_text_only")
        item_types = (
            "、".join(str(item) for item in output_item_types)
            if isinstance(output_item_types, list) and output_item_types
            else "无"
        )
        return [
            f"response.status: {response_status or '无/不适用'}",
            f"incomplete_details.reason: {incomplete_reason or '无'}",
            f"finish_reason: {finish_reason or '无/不适用'}",
            f"output item 类型: {item_types}",
            f"仅返回普通文本: {'是' if plain_text_only is True else '否'}",
        ]


class ProviderSettingsMixin:
    """Provider inventory, editor, compatibility probe and lifecycle actions."""

    _provider_dialog: ProviderEditorDialog | None
    _provider_test_workers: list[AsyncTaskThread]
    operation_registry: AsyncOperationRegistry | None
    _provider_action_busy: bool

    # The presentation aliases preserve the historical private helpers used by
    # tests and by older development integrations.
    _provider_ref = staticmethod(SettingsPresenter.provider_ref)
    _is_archived = staticmethod(SettingsPresenter.is_archived)
    _safe_error = staticmethod(SettingsPresenter.safe_error)
    _provider_error_fields = staticmethod(SettingsPresenter.provider_error_fields)
    _provider_response_fields = staticmethod(SettingsPresenter.provider_response_fields)

    def _provider_registry(self: Any) -> object | None:
        for name in ("provider_registry", "custom_provider_registry", "providers"):
            registry = getattr(self.service, name, None)
            if registry is not None and callable(getattr(registry, "list", None)):
                return cast(object, registry)
        return None

    def _provider_method(self: Any, names: tuple[str, ...]) -> Any:
        for name in names:
            method = getattr(self.service, name, None)
            if callable(method):
                return method
        registry = self._provider_registry()
        if registry is not None:
            registry_names = {
                "list_custom_providers": "list",
                "create_custom_provider": "create",
                "create_provider": "create",
                "update_custom_provider": "update",
                "update_provider": "update",
                "archive_custom_provider": "archive",
                "restore_custom_provider": "restore",
                "replace_custom_provider_endpoint": "replace_endpoint",
                "replace_provider_endpoint": "replace_endpoint",
                "delete_custom_provider": "delete",
                "delete_custom_provider_key": "delete_key",
                "delete_key": "delete_key",
                "rotate_custom_provider_key": "rotate_key",
                "rotate_provider_key": "rotate_key",
            }
            for name in names:
                method = getattr(registry, registry_names.get(name, name), None)
                if callable(method):
                    return method
        return None

    def _load_custom_providers(self: Any) -> None:
        include_archived = bool(self.provider_filter.currentData())
        try:
            method = self._provider_method(("list_custom_providers",))
            if method is not None:
                try:
                    items = method(include_archived=include_archived)
                except TypeError:
                    items = method(include_archived)
            else:
                items = []
            items = list(items)
            key_state = {
                self._provider_ref(item): self._provider_has_key(self._provider_ref(item))
                for item in items
            }
            self.provider_table_model.set_items(items, key_state)
            self.provider_store_error.hide()
        except Exception as error:
            self.provider_table_model.set_items([])
            self.provider_store_error.setText(
                f"无法读取自定义 Provider 配置：{self._safe_error(error)}"
            )
            self.provider_store_error.show()
        self._update_provider_actions()
        if hasattr(self, "provider"):
            self._refresh_default_provider_choices(str(self.provider.currentData() or ""))

    def _refresh_default_provider_choices(self: Any, selected_ref: str) -> None:
        connections = provider_connections(self.service)
        self.provider.blockSignals(True)
        try:
            self.provider.clear()
            self._provider_default_models = {}
            for connection in connections:
                protocol = provider_protocol_text(connection.protocol)
                self.provider.addItem(
                    f"{connection.display_name} · {protocol}", connection.provider_ref
                )
                self._provider_default_models[connection.provider_ref] = connection.default_model
            selected_index = self.provider.findData(selected_ref)
            if selected_index < 0:
                selected_index = self.provider.findData("openai")
            self.provider.setCurrentIndex(max(0, selected_index))
        finally:
            self.provider.blockSignals(False)

    def _default_provider_activated(self: Any, _index: int) -> None:
        provider_ref = str(self.provider.currentData() or "")
        default_model = self._provider_default_models.get(provider_ref, "")
        if default_model:
            self.model.setText(default_model)

    def _provider_has_key(self: Any, provider_ref: str) -> bool:
        method = self._provider_method(
            ("provider_has_key", "custom_provider_has_key", "has_custom_provider_key")
        )
        if method is not None:
            return bool(method(provider_ref))
        registry = self._provider_registry()
        registry_has_key = getattr(registry, "has_key", None)
        if callable(registry_has_key):
            return bool(registry_has_key(provider_ref))
        return False

    def _selected_provider(self: Any) -> object | None:
        selection = self.provider_table.selectionModel().selectedRows()
        if not selection:
            return None
        return cast(object | None, self.provider_table_model.item(selection[0].row()))

    def _selected_provider_ref(self: Any) -> str:
        selected = self._selected_provider()
        return self._provider_ref(selected) if selected is not None else ""

    def _update_provider_actions(self: Any) -> None:
        buttons = (
            self.edit_provider_button,
            self.replace_provider_button,
            self.rotate_provider_key_button,
            self.delete_provider_key_button,
            self.archive_provider_button,
            self.restore_provider_button,
            self.delete_provider_button,
        )
        if self._provider_action_busy:
            for button in buttons:
                button.setEnabled(False)
            return
        selected = self._selected_provider()
        active = selected is not None and not self._is_archived(selected)
        archived = selected is not None and self._is_archived(selected)
        for button in buttons[:4]:
            button.setEnabled(bool(active))
        self.archive_provider_button.setEnabled(bool(active))
        self.restore_provider_button.setEnabled(bool(archived))
        self.delete_provider_button.setEnabled(bool(archived))

    def _add_custom_provider(self: Any) -> None:
        self._open_provider_dialog(None, focus_target=self.add_provider_button)

    def _edit_selected_provider(self: Any) -> None:
        selected = self._selected_provider()
        if selected is not None:
            self._open_provider_dialog(selected, focus_target=self.edit_provider_button)

    def _edit_custom_provider(self: Any, provider_ref: str) -> None:
        for row in range(self.provider_table_model.rowCount()):
            if self.provider_table_model.provider_ref(row) == provider_ref:
                self.provider_table.selectRow(row)
                self._edit_selected_provider()
                return

    def _open_provider_dialog(
        self: Any, profile: object | None, *, focus_target: QWidget | None = None
    ) -> None:
        target = focus_target or self.add_provider_button
        self._provider_dialog = ProviderEditorDialog(
            existing=profile,
            has_key=self._provider_has_key(self._provider_ref(profile)) if profile else False,
            parent=self,
        )
        self._provider_dialog.save_requested.connect(self._save_provider_dialog)
        self._provider_dialog.test_requested.connect(self._test_provider_dialog)
        self._provider_dialog.finished.connect(
            lambda _code: self._provider_dialog_finished(target)
        )
        self._provider_dialog.open()

    def _provider_dialog_finished(self: Any, focus_target: QWidget) -> None:
        self._clear_provider_dialog()
        focus_target.setFocus(Qt.FocusReason.OtherFocusReason)

    def _clear_provider_dialog(self: Any) -> None:
        self._provider_dialog = None

    def _save_provider_dialog(self: Any) -> None:
        dialog = self._provider_dialog
        if dialog is None:
            return
        values = dialog.validate_fields(
            require_api_key=not bool(dialog.provider_ref) or dialog.replacement_mode
        )
        if values is None:
            return
        dialog.set_busy(True)
        provider_ref = dialog.provider_ref
        replacement_mode = dialog.replacement_mode
        worker = AsyncTaskThread(
            lambda _emit: self._save_provider_operation(provider_ref, replacement_mode, values)
        )
        self._register_provider_worker(worker)
        worker.completed.connect(self._provider_save_completed)
        worker.failed.connect(self._provider_save_failed)
        worker.start()

    async def _save_provider_operation(
        self: Any,
        provider_ref: str,
        replacement_mode: bool,
        values: ProviderFormValues,
    ) -> tuple[str, str]:
        changed_ref = provider_ref
        if not provider_ref:
            created = self._provider_call(
                ("create_custom_provider", "create_provider"),
                display_name=values.display_name,
                protocol=values.protocol,
                base_url=values.base_url,
                default_model=values.default_model,
                api_key=values.api_key,
            )
            if inspect.isawaitable(created):
                created = await created
            changed_ref = self._provider_ref(created)
            message = f"已添加 Provider：{values.display_name}。"
        elif replacement_mode:
            replacement = self._provider_call(
                ("replace_custom_provider_endpoint", "replace_provider_endpoint"),
                provider_ref,
                protocol=values.protocol,
                base_url=values.base_url,
                default_model=values.default_model,
                display_name=values.display_name,
                api_key=values.api_key,
            )
            if inspect.isawaitable(replacement):
                replacement = await replacement
            changed_ref = self._provider_ref(replacement)
            message = "已创建新 Provider，并归档旧端点配置。"
        else:
            updated = self._provider_call(
                ("update_custom_provider", "update_provider"),
                provider_ref,
                display_name=values.display_name,
                default_model=values.default_model,
            )
            if inspect.isawaitable(updated):
                await updated
            if values.api_key.strip():
                rotated = self._provider_call(
                    ("rotate_custom_provider_key", "rotate_provider_key"),
                    provider_ref,
                    api_key=values.api_key,
                )
                if inspect.isawaitable(rotated):
                    await rotated
            message = f"已更新 Provider：{values.display_name}。"
        return changed_ref, message

    def _provider_save_completed(self: Any, result: object) -> None:
        if not isinstance(result, tuple) or len(result) != 2:
            self._provider_save_failed("Provider 保存结果格式无效", "")
            return
        changed_ref, message = str(result[0]), str(result[1])
        dialog = self._provider_dialog
        if dialog is not None:
            dialog.set_busy(False)
            dialog.accept()
        self._load_custom_providers()
        self.credentials_changed.emit(changed_ref)
        self.message.show_message(message, severity="success")

    def _provider_save_failed(self: Any, message: str, _traceback: str) -> None:
        if self._provider_dialog is not None:
            self._provider_dialog.set_busy(False)
        self.message.show_message(
            f"保存 Provider 失败：{self._safe_error(ValueError(message))}", severity="danger"
        )

    def _replace_selected_provider(self: Any) -> None:
        selected = self._selected_provider()
        if selected is None:
            return
        self._open_provider_dialog(selected)
        if self._provider_dialog is not None:
            self._provider_dialog._enable_replacement()

    def _test_provider_dialog(self: Any, values: object) -> None:
        dialog = self._provider_dialog
        if dialog is None or not isinstance(values, ProviderFormValues):
            return
        answer = QMessageBox.question(
            dialog,
            "确认测试 Provider",
            "兼容性测试将发送一次可能计费的最小工具调用请求，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        dialog.set_busy(True)
        # Endpoint replacement probes the new transient connection, not the
        # old endpoint represented by the existing provider ref.
        provider_ref = "" if dialog.replacement_mode else dialog.provider_ref
        worker = AsyncTaskThread(
            lambda _emit: self._run_provider_compatibility(provider_ref, values)
        )
        self._register_provider_worker(worker)
        worker.completed.connect(self._provider_test_completed)
        worker.failed.connect(self._provider_test_failed)
        worker.start()

    async def _run_provider_compatibility(
        self: Any, provider_ref: str, values: ProviderFormValues
    ) -> object:
        method = self._provider_method(("test_provider_compatibility",))
        if method is None:
            raise RuntimeError("当前版本的应用服务尚未提供 Provider 兼容性测试接口")
        candidate = {
            "provider_ref": provider_ref or None,
            "protocol": values.protocol,
            "base_url": values.base_url,
            "model": values.default_model,
            "default_model": values.default_model,
            "api_key": values.api_key or None,
        }
        try:
            signature = inspect.signature(method)
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
            kwargs = candidate if accepts_kwargs else {
                key: value for key, value in candidate.items() if key in signature.parameters
            }
            result = method(**kwargs)
        except (TypeError, ValueError):
            result = method(
                provider_ref or None,
                values.protocol,
                values.base_url,
                values.default_model,
                values.api_key or None,
            )
        if inspect.isawaitable(result):
            return await result
        return result

    def _provider_test_completed(self: Any, result: object) -> None:
        dialog = self._provider_dialog
        if dialog is None:
            return
        success = bool(
            result
            if isinstance(result, bool)
            else getattr(result, "success", getattr(result, "compatible", True))
        )
        if isinstance(result, dict):
            success = bool(result.get("success", result.get("compatible", success)))
            detail = str(result.get("message", result.get("detail", "兼容性测试通过")))
            error_details = result.get("error_details")
            response_diagnostics = result.get("response_diagnostics")
        else:
            detail = str(getattr(result, "message", "兼容性测试通过"))
            error_details = getattr(result, "error_details", None)
            response_diagnostics = getattr(result, "response_diagnostics", None)
        dialog.set_busy(False)
        prefix = "兼容性测试通过：" if success else "兼容性测试失败："
        message = prefix + detail
        if not success:
            provider_fields = self._provider_error_fields(error_details)
            if provider_fields:
                message += "\n服务商返回（已脱敏）：\n" + "\n".join(provider_fields)
        diagnostic_fields = self._provider_response_fields(response_diagnostics)
        if diagnostic_fields:
            message += "\n响应诊断（不含正文）：\n" + "\n".join(diagnostic_fields)
        dialog.show_test_result(success, message)

    def _provider_test_failed(self: Any, message: str, _traceback: str) -> None:
        if self._provider_dialog is not None:
            self._provider_dialog.set_busy(False)
            self._provider_dialog.show_test_result(
                False,
                f"兼容性测试失败：{self._safe_error(ValueError(message))}",
            )

    def _register_provider_worker(self: Any, worker: AsyncTaskThread) -> None:
        """Track a settings worker in the window registry when available.

        SettingsPage is also used directly by widget tests and lightweight
        integrations.  Those callers have no parent registry, so the legacy
        page-local cleanup path remains the fallback.
        """

        self._provider_test_workers.append(worker)
        registry = getattr(self, "operation_registry", None)
        if registry is not None:
            registry.track(worker, self._remove_provider_worker)
        else:
            worker.finished.connect(lambda: self._remove_provider_worker(worker))

    def _remove_provider_worker(self: Any, worker: AsyncTaskThread) -> None:
        if worker in self._provider_test_workers:
            self._provider_test_workers.remove(worker)
        # AsyncOperationRegistry owns deletion after invoking this callback.
        # The standalone SettingsPage path retains its historical behavior.
        if getattr(self, "operation_registry", None) is None:
            worker.deleteLater()

    def _provider_call(
        self: Any, names: tuple[str, ...], *args: object, **kwargs: object
    ) -> object:
        method = self._provider_method(names)
        if method is None:
            raise RuntimeError("当前版本的应用服务尚未提供自定义 Provider 管理接口")
        return method(*args, **kwargs)

    def _archive_selected_provider(self: Any) -> None:
        ref = self._selected_provider_ref()
        if not ref:
            return
        if str(self.provider.currentData()) == ref:
            self.message.show_message(
                "当前默认 Provider 不能归档，请先切换默认 Provider。", severity="warning"
            )
            return
        if (
            QMessageBox.question(
                self, "归档 Provider", "归档后会保留配置以便历史任务恢复，是否继续？"
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._run_provider_action(("archive_custom_provider", "archive"), ref, "Provider 已归档。")

    def _restore_selected_provider(self: Any) -> None:
        ref = self._selected_provider_ref()
        if ref:
            self._run_provider_action(
                ("restore_custom_provider", "restore"), ref, "Provider 已恢复。"
            )

    def _delete_selected_provider(self: Any) -> None:
        ref = self._selected_provider_ref()
        if not ref:
            return
        if (
            QMessageBox.question(
                self,
                "永久删除 Provider",
                "仅删除已归档且未被历史任务引用的配置，是否继续？",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._run_provider_action(
            ("delete_custom_provider", "delete"), ref, "Provider 已永久删除。"
        )

    def _rotate_selected_provider_key(self: Any) -> None:
        selected = self._selected_provider()
        if selected is None:
            return
        self._open_provider_dialog(selected)
        if self._provider_dialog is not None:
            self._provider_dialog.api_key.setFocus(Qt.FocusReason.OtherFocusReason)

    def _delete_selected_provider_key(self: Any) -> None:
        ref = self._selected_provider_ref()
        if not ref:
            return
        if (
            QMessageBox.question(
                self, "删除 API Key", "删除后该 Provider 将无法运行，是否继续？"
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._run_provider_action(
            ("delete_custom_provider_key", "delete_key"), ref, "Provider API Key 已删除。"
        )

    def _run_provider_action(
        self: Any, names: tuple[str, ...], ref: str, success_message: str
    ) -> None:
        self._set_provider_action_busy(True)
        worker = AsyncTaskThread(lambda _emit: self._execute_provider_action(names, ref))
        self._register_provider_worker(worker)
        worker.completed.connect(
            lambda _value: self._finish_provider_action(ref, success_message)
        )
        worker.failed.connect(self._provider_action_failed)
        worker.start()

    async def _execute_provider_action(self: Any, names: tuple[str, ...], ref: str) -> object:
        result = self._provider_call(names, ref)
        if inspect.isawaitable(result):
            return await result
        return result

    def _finish_provider_action(self: Any, ref: str, success_message: str) -> None:
        self._set_provider_action_busy(False)
        self._load_custom_providers()
        self.credentials_changed.emit(ref)
        self.message.show_message(success_message, severity="success")

    def _provider_action_failed(self: Any, message: str, _traceback: str) -> None:
        self._set_provider_action_busy(False)
        self.message.show_message(
            f"操作失败：{self._safe_error(ValueError(message))}", severity="danger"
        )

    def _set_provider_action_busy(self: Any, busy: bool) -> None:
        self._provider_action_busy = busy
        self.provider_filter.setEnabled(not busy)
        self.provider_table.setEnabled(not busy)
        self.add_provider_button.setEnabled(not busy)
        self._update_provider_actions()


class CredentialSettingsMixin:
    """Built-in API credential operations kept separate from Provider CRUD."""

    def _save_key(self: Any, provider: str) -> None:
        field = self.key_fields[provider]
        try:
            self.service.credentials.set(provider, field.text())
        except Exception as error:
            self.message.show_message(f"保存失败：{error}", severity="danger")
            return
        field.clear()
        field.setPlaceholderText("已安全保存；输入新 Key 可替换")
        self.credentials_changed.emit(provider)
        self.message.show_message(f"{provider} API Key 已保存到系统凭据库。", severity="success")

    def _delete_key(self: Any, provider: str) -> None:
        try:
            self.service.credentials.delete(provider)
        except Exception as error:
            self.message.show_message(f"删除失败：{error}", severity="danger")
            return
        self.key_fields[provider].clear()
        self.key_fields[provider].setPlaceholderText("输入 API Key")
        self.credentials_changed.emit(provider)
        self.message.show_message(f"已删除 {provider} API Key。", severity="success")


class PreferencesSettingsMixin:
    """Default review, theme, motion and local directory operations."""

    def apply_preferences(self: Any) -> None:
        self.provider.setCurrentIndex(
            max(0, self.provider.findData(self.preferences.default_provider))
        )
        self.model.setText(self.preferences.default_model)
        self.default_rubric.set_path(self.preferences.default_rubric)
        self.external_search.setChecked(self.preferences.external_search)
        self.theme.setCurrentIndex(max(0, self.theme.findData(self.preferences.theme)))
        self.motion.setCurrentIndex(max(0, self.motion.findData(self.preferences.motion)))

    def show_preferences_saved(self: Any) -> None:
        self.message.show_message("设置已保存。", severity="success")

    def show_preferences_error(self: Any, message: str) -> None:
        self.message.show_message(f"设置保存失败：{message}", severity="danger")

    def _browse_rubric(self: Any) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择默认 Rubric", "", "YAML (*.yaml *.yml)"
        )
        if path:
            self.default_rubric.set_path(path)

    def _save_preferences(self: Any) -> None:
        self.preferences.default_provider = str(self.provider.currentData())
        self.preferences.default_model = self.model.text().strip()
        rubric = self.default_rubric.path()
        if rubric is not None:
            if not rubric.is_file():
                message = "请选择存在的 Rubric YAML 文件"
                self.default_rubric.set_invalid(message)
                self.message.show_message(message, severity="danger")
                return
            validation = self.service.validate_rubric(
                rubric, profile_path=self.profile_path
            )
            if not validation.valid:
                message = "；".join(validation.errors)
                self.default_rubric.set_invalid(message)
                self.message.show_message(message, severity="danger")
                return
        self.default_rubric.set_invalid(None)
        self.preferences.default_rubric = (
            str(rubric.resolve()) if rubric and rubric.is_file() else ""
        )
        self.preferences.external_search = self.external_search.isChecked()
        self.preferences.theme = str(self.theme.currentData())
        self.preferences.motion = str(self.motion.currentData())
        self.preferences_changed.emit()
        self.theme_changed.emit(self.preferences.theme)

    def _open_directory(self: Any, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
