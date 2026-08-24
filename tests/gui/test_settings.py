from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from paper_reviewer.application.app_state import AppPaths, GuiPreferences
from paper_reviewer.domain.provider import CustomProviderProfile, ModelApiProtocol
from paper_reviewer.gui.icons import FluentIconService
from paper_reviewer.gui.pages.settings import SettingsPage
from paper_reviewer.gui.provider_widgets import ProviderEditorDialog, ProviderTableModel
from paper_reviewer.gui.theme import FluentThemeManager


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
