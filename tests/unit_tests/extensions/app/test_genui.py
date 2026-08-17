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

    def test_carousel_builds_component_payload(self):
        component = genui.carousel("media", ["https://example.com/a.jpg", "https://example.com/b.jpg"])
        assert component == {
            "id": "media",
            "component": "Carousel",
            "content": ["https://example.com/a.jpg", "https://example.com/b.jpg"],
        }

    def test_carousel_omits_unset_optional_fields(self):
        component = genui.carousel("media", ["https://example.com/a.jpg"])
        assert "autoplay" not in component
        assert "draggable" not in component
        assert "styles" not in component

    def test_summary_card_builds_create_and_update_pair(self):
        messages = genui.summary_card("surface-1", title="Title", body="Body")
        assert len(messages) == 2
        assert "createSurface" in messages[0]
        update = messages[1]["updateComponents"]
        ids = {c["id"] for c in update["components"]}
        assert ids == {"root", "content", "title", "divider", "body"}
        assert "linkButton" not in ids

    def test_summary_card_with_link_url_adds_open_url_button(self):
        messages = genui.summary_card(
            "surface-1", title="Title", body="Body", link_url="https://example.com/book", link_label="Continue"
        )
        components = {c["id"]: c for c in messages[1]["updateComponents"]["components"]}
        assert "linkButton" in components
        assert components["linkButton"]["action"]["functionCall"] == {
            "call": "openUrl",
            "args": {"url": "https://example.com/book"},
        }
        assert components["linkText"]["text"] == "Continue"

    def test_map_card_builds_create_and_update_pair(self):
        messages = genui.map_card("surface-1", "Bangkok", "https://example.com/map-embed?data=...")
        assert len(messages) == 2
        assert "createSurface" in messages[0]
        components = {c["id"]: c for c in messages[1]["updateComponents"]["components"]}
        assert components["map"]["component"] == "MapWeb"
        assert components["map"]["url"] == "https://example.com/map-embed?data=..."
        assert "caption" not in components

    def test_map_card_with_caption_adds_caption_text(self):
        messages = genui.map_card("surface-1", "Bangkok", "https://example.com/map-embed?data=...", caption="- Grand Palace")
        components = {c["id"]: c for c in messages[1]["updateComponents"]["components"]}
        assert components["caption"]["text"] == "- Grand Palace"

    def test_map_web_builds_component_payload(self):
        component = genui.map_web("map", "https://example.com/map-embed?data=...")
        assert component == {"id": "map", "component": "MapWeb", "url": "https://example.com/map-embed?data=..."}

    def test_hotel_gallery_card_builds_create_and_update_pair(self):
        messages = genui.hotel_gallery_card(
            "surface-1",
            "Bali hotels",
            [
                {
                    "name": "The Ritz-Carlton, Bali",
                    "price_per_night": "$548",
                    "rating": 4.6,
                    "reviews": 4547,
                    "hotel_class": "5-star hotel",
                    "image_urls": ["https://example.com/a.jpg", "https://example.com/b.jpg"],
                    "link": "https://example.com/ritz",
                    "description": "Upscale property with a spa.",
                }
            ],
        )
        assert len(messages) == 2
        assert "createSurface" in messages[0]
        components = {c["id"]: c for c in messages[1]["updateComponents"]["components"]}
        assert components["hotel0Name"]["text"] == "The Ritz-Carlton, Bali"
        assert "$548/night" in components["hotel0Subtitle"]["text"]
        assert "★ 4.6" in components["hotel0Subtitle"]["text"]
        assert "5-star hotel" in components["hotel0Subtitle"]["text"]
        assert components["hotel0Media"]["component"] == "Carousel"
        assert components["hotel0Media"]["content"] == ["https://example.com/a.jpg", "https://example.com/b.jpg"]
        assert components["hotel0Desc"]["text"] == "Upscale property with a spa."
        assert components["hotel0Button"]["action"]["functionCall"] == {
            "call": "openUrl",
            "args": {"url": "https://example.com/ritz"},
        }
        assert components["hotel0ButtonText"]["text"] == "View Hotel"

    def test_hotel_gallery_card_renders_plain_image_for_a_single_photo(self):
        messages = genui.hotel_gallery_card(
            "surface-1", "Bali hotels", [{"name": "Solo Photo Hotel", "image_urls": ["https://example.com/only.jpg"]}]
        )
        components = {c["id"]: c for c in messages[1]["updateComponents"]["components"]}
        assert components["hotel0Media"]["component"] == "Image"
        assert components["hotel0Media"]["url"] == "https://example.com/only.jpg"

    def test_hotel_gallery_card_omits_optional_fields_when_absent(self):
        messages = genui.hotel_gallery_card("surface-1", "Bali hotels", [{"name": "Mystery Hotel"}])
        components = {c["id"]: c for c in messages[1]["updateComponents"]["components"]}
        assert components["hotel0Name"]["text"] == "Mystery Hotel"
        assert "hotel0Subtitle" not in components
        assert "hotel0Media" not in components
        assert "hotel0Desc" not in components
        assert "hotel0Button" not in components

    def test_hotel_gallery_card_adds_show_more_button_when_more_count_positive(self):
        messages = genui.hotel_gallery_card(
            "surface-1", "Bali hotels", [{"name": "Hotel A"}], more_count=5
        )
        components = {c["id"]: c for c in messages[1]["updateComponents"]["components"]}
        assert components["moreButtonText"]["text"] == "Show more"
        assert components["moreButton"]["action"]["event"]["name"] == "show_more_hotels"
        assert "moreButton" in messages[1]["updateComponents"]["components"][0]["children"]

    def test_hotel_gallery_card_omits_show_more_button_when_more_count_zero(self):
        messages = genui.hotel_gallery_card("surface-1", "Bali hotels", [{"name": "Hotel A"}])
        components = {c["id"]: c for c in messages[1]["updateComponents"]["components"]}
        assert "moreButton" not in components
        assert "moreButtonText" not in components

    def test_flight_gallery_card_builds_create_and_update_pair(self):
        messages = genui.flight_gallery_card(
            "surface-1",
            "Tokyo flights",
            [
                {
                    "airline": "Singapore Airlines",
                    "airline_logo": "https://example.com/sq-logo.png",
                    "price": "$412",
                    "stops_label": "Nonstop",
                    "duration": "7h 30m",
                    "travel_class": "Economy",
                    "departure_airport": "SIN",
                    "departure_time": "Sep 10, 22:05",
                    "arrival_airport": "NRT",
                    "arrival_time": "Sep 11, 06:15",
                    "link": "https://www.google.com/travel/flights?q=test",
                }
            ],
        )
        assert len(messages) == 2
        assert "createSurface" in messages[0]
        components = {c["id"]: c for c in messages[1]["updateComponents"]["components"]}
        assert components["flight0Name"]["text"] == "Singapore Airlines"
        assert "$412" in components["flight0Subtitle"]["text"]
        assert "Nonstop" in components["flight0Subtitle"]["text"]
        assert "7h 30m" in components["flight0Subtitle"]["text"]
        assert components["flight0Logo"]["component"] == "Image"
        assert components["flight0Logo"]["url"] == "https://example.com/sq-logo.png"
        assert "SIN" in components["flight0Route"]["text"]
        assert "NRT" in components["flight0Route"]["text"]
        assert components["flight0Button"]["action"]["functionCall"] == {
            "call": "openUrl",
            "args": {"url": "https://www.google.com/travel/flights?q=test"},
        }
        assert components["flight0ButtonText"]["text"] == "View Flights"

    def test_flight_gallery_card_omits_logo_when_absent(self):
        messages = genui.flight_gallery_card("surface-1", "Tokyo flights", [{"airline": "Mystery Air"}])
        components = {c["id"]: c for c in messages[1]["updateComponents"]["components"]}
        assert "flight0Logo" not in components
        assert components["flight0Name"]["text"] == "Mystery Air"

    def test_flight_gallery_card_omits_optional_fields_when_absent(self):
        messages = genui.flight_gallery_card("surface-1", "Tokyo flights", [{"airline": "Mystery Air"}])
        components = {c["id"]: c for c in messages[1]["updateComponents"]["components"]}
        assert components["flight0Name"]["text"] == "Mystery Air"
        assert "flight0Subtitle" not in components
        assert "flight0Route" not in components
        assert "flight0Button" not in components

    def test_flight_gallery_card_adds_show_more_button_when_more_count_positive(self):
        messages = genui.flight_gallery_card("surface-1", "Tokyo flights", [{"airline": "Airline A"}], more_count=5)
        components = {c["id"]: c for c in messages[1]["updateComponents"]["components"]}
        assert components["moreButtonText"]["text"] == "Show more"
        assert components["moreButton"]["action"]["event"]["name"] == "show_more_flights"
        assert "moreButton" in messages[1]["updateComponents"]["components"][0]["children"]

    def test_flight_gallery_card_omits_show_more_button_when_more_count_zero(self):
        messages = genui.flight_gallery_card("surface-1", "Tokyo flights", [{"airline": "Airline A"}])
        components = {c["id"]: c for c in messages[1]["updateComponents"]["components"]}
        assert "moreButton" not in components
        assert "moreButtonText" not in components


class TestFormHelpers:
    def test_choice_picker_defaults(self):
        picker = genui.choice_picker("level", [("Beginner", "Beginner")])
        assert picker["options"] == [{"label": "Beginner", "value": "Beginner"}]
        assert picker["variant"] == "mutuallyExclusive"

    def test_button_builds_context_paths(self):
        btn = genui.button("submit", "label", "submit_action", context_paths={"level": "level.value"})
        assert btn["action"]["event"]["name"] == "submit_action"
        assert btn["action"]["event"]["context"] == {"level": {"path": "level.value"}}

    def test_open_url_button_uses_function_call_not_event(self):
        btn = genui.open_url_button("openBtn", "openText", "https://example.com")
        assert btn["action"] == {"functionCall": {"call": "openUrl", "args": {"url": "https://example.com"}}}
        assert "event" not in btn["action"]

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
        # Single unnamed group ("Preferences") still wraps fields in a group card.
        assert component_ids == [
            "root",
            "title",
            "group0Card",
            "group0Content",
            "group0Title",
            "brand",
            "submitCard",
            "submit",
            "submitText",
        ]

    def test_form_groups_fields_by_category(self):
        fields = [
            genui.text_field("brand", label="Brand"),
            genui.slider("budget", value=100, min_value=0, max_value=500, label="Budget"),
        ]
        messages = genui.form(
            "surface-1",
            title="Preferences",
            fields=fields,
            field_groups=[("Details", [fields[0]]), ("Budget", [fields[1]])],
            submit_label="Submit",
            action_name="submit_prefs",
            field_paths={"brand": "/brand/value", "budget": "/budget/value"},
        )
        component_ids = [c["id"] for c in messages[1]["updateComponents"]["components"]]
        assert "group0Card" in component_ids
        assert "group1Card" in component_ids
        titles = [c for c in messages[1]["updateComponents"]["components"] if c["id"] in ("group0Title", "group1Title")]
        assert {c["text"] for c in titles} == {"Details", "Budget"}

    def test_form_seeds_data_model_with_field_defaults(self):
        fields = [genui.text_field("brand", label="Brand")]
        messages = genui.form(
            "surface-1",
            title="Preferences",
            fields=fields,
            submit_label="Submit",
            action_name="submit_prefs",
            field_paths={"brand": "/brand/value"},
            field_defaults={"brand": "Yonex"},
        )
        # updateDataModel messages land between createSurface and updateComponents.
        assert messages[0]["createSurface"]
        assert messages[1]["updateDataModel"] == {"surfaceId": "surface-1", "path": "/brand/value", "value": "Yonex"}
        assert "updateComponents" in messages[-1]
