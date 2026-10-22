"""Тесты сохранения сессии: кэш шрифтов в state.json не нужен."""

from __future__ import annotations

from agent.browser import trim_storage_state


def test_font_entries_are_removed() -> None:
    state = {
        "cookies": [{"name": "hhtoken", "value": "secret"}],
        "origins": [
            {
                "origin": "https://hh.ru",
                "localStorage": [
                    {"name": "magritteFontsNewFont", "value": "x" * 100},
                    {"name": "xhhFontsFont", "value": "y" * 100},
                    {"name": "magritteFontsNewFontVersion", "value": "0.0.6"},
                    {"name": "ss_incoming_params", "value": "[]"},
                ],
            }
        ],
    }

    trimmed = trim_storage_state(state)

    assert [item["name"] for item in trimmed["origins"][0]["localStorage"]] == [
        "magritteFontsNewFontVersion",
        "ss_incoming_params",
    ]
    assert trimmed["cookies"] == state["cookies"]


def test_original_state_is_not_modified() -> None:
    state = {
        "cookies": [],
        "origins": [{"origin": "https://hh.ru", "localStorage": [{"name": "f", "value": "1"}]}],
    }

    trim_storage_state(state)

    assert state["origins"][0]["localStorage"] == [{"name": "f", "value": "1"}]


def test_missing_origins_is_fine() -> None:
    assert trim_storage_state({"cookies": []}) == {"cookies": [], "origins": []}
