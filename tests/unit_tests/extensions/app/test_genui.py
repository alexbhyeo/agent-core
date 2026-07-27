# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Unit tests for openjiuwen.extensions.app.genui."""

import pytest

from openjiuwen.extensions.app import genui


class TestSurfaceMessages:
    def test_create_surface_uses_configured_catalog_by_default(self):
        message = genui.create_surface("surface-1")
        assert message["version"] == genui.A2UI_VERSION
        assert message["createSurface"]["surfaceId"] == "surface-1"
        assert message["createSurface"]["catalogId"]

    def test_create_surface_accepts_explicit_catalog_and_theme(self):
        message = genui.create_surface(
            "surface-1", catalog_id="custom-catalog", theme={"dark": True}, send_data_model=True
        )
        payload = message["createSurface"]
        assert payload["catalogId"] == "custom-catalog"
        assert payload["theme"] == {"dark": True}
        assert payload["sendDataModel"] is True

    def test_update_components_requires_root_component(self):
        with pytest.raises(ValueError, match="root"):
            genui.update_components("surface-1", [{"id": "not-root", "component": "Text"}])

    def test_update_components_accepts_root_component(self):
        components = [{"id": "root", "component": "Column", "children": []}]
        message = genui.update_components("surface-1", components)
        assert message["updateComponents"]["surfaceId"] == "surface-1"
        assert message["updateComponents"]["components"] == components

    def test_delete_surface(self):
        message = genui.delete_surface("surface-1")
        assert message == {"version": genui.A2UI_VERSION, "deleteSurface": {"surfaceId": "surface-1"}}


class TestBasicCatalogHelpers:
    def test_text_default_variant(self):
        assert genui.text("t1", "hello") == {
            "id": "t1",
            "component": "Text",
            "text": "hello",
            "variant": "body",
        }

    def test_column_omits_unset_optional_fields(self):
        component = genui.column("root", ["a", "b"])
        assert "justify" not in component
        assert "align" not in component
        assert component["children"] == ["a", "b"]

    def test_summary_card_builds_create_and_update_pair(self):
        messages = genui.summary_card("surface-1", title="Title", body="Body")
        assert len(messages) == 2
        assert "createSurface" in messages[0]
        update = messages[1]["updateComponents"]
        ids = {c["id"] for c in update["components"]}
        assert ids == {"root", "title", "divider", "body"}


class TestFormHelpers:
    def test_choice_picker_defaults(self):
        picker = genui.choice_picker("level", [("Beginner", "Beginner")])
        assert picker["options"] == [{"label": "Beginner", "value": "Beginner"}]
        assert picker["variant"] == "mutuallyExclusive"

    def test_button_builds_context_paths(self):
        btn = genui.button("submit", "label", "submit_action", context_paths={"level": "level.value"})
        assert btn["action"]["event"]["name"] == "submit_action"
        assert btn["action"]["event"]["context"] == {"level": {"path": "level.value"}}

    def test_form_builds_create_and_update_with_submit_button(self):
        fields = [genui.text_field("brand", label="Brand")]
        messages = genui.form(
            "surface-1",
            title="Preferences",
            fields=fields,
            submit_label="Submit",
            action_name="submit_prefs",
            field_paths={"brand": "brand.value"},
        )
        assert len(messages) == 2
        assert messages[0]["createSurface"]["sendDataModel"] is True
        component_ids = [c["id"] for c in messages[1]["updateComponents"]["components"]]
        assert component_ids == ["root", "title", "brand", "submit", "submitText"]
