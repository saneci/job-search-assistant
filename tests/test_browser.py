"""Тесты сохранения сессии и определения капчи (без браузера)."""

from __future__ import annotations

from agent.browser import detect_captcha, trim_storage_state, wait_for_captcha_solved


class FakeLocator:
    """Заглушка Locator: видимость и текст задаются в тесте."""

    def __init__(self, visible: bool = False, text: str = "") -> None:
        self._visible = visible
        self._text = text

    @property
    def first(self) -> "FakeLocator":
        return self

    def count(self) -> int:
        return 1 if self._visible else 0

    def is_visible(self) -> bool:
        return self._visible

    def inner_text(self) -> str:
        return self._text


class FakePage:
    """Страница, у которой капча исчезает через заданное число опросов."""

    def __init__(
        self,
        url: str = "https://hh.ru/vacancy/1",
        dialog_text: str = "",
        captcha_ticks: int = 0,
    ) -> None:
        self.url = url
        self.dialog_text = dialog_text
        self.captcha_ticks = captcha_ticks
        self.waits = 0

    def locator(self, selector: str) -> FakeLocator:
        if selector in ("[role='dialog']", "[data-qa='modal-content']") and self.captcha_ticks > 0:
            return FakeLocator(visible=True, text=self.dialog_text)
        return FakeLocator()

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits += 1
        if self.captcha_ticks > 0:
            self.captcha_ticks -= 1


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


def test_letter_drafts_are_removed() -> None:
    """Черновик письма из localStorage в файл сессии не попадает."""
    state = {
        "cookies": [],
        "origins": [
            {
                "origin": "https://hh.ru",
                "localStorage": [
                    {"name": "vacancy_response_letter_138739107", "value": "Здравствуйте!"},
                    {"name": "hh_theme", "value": "light"},
                ],
            }
        ],
    }

    trimmed = trim_storage_state(state)

    assert [item["name"] for item in trimmed["origins"][0]["localStorage"]] == ["hh_theme"]


def test_original_state_is_not_modified() -> None:
    state = {
        "cookies": [],
        "origins": [{"origin": "https://hh.ru", "localStorage": [{"name": "f", "value": "1"}]}],
    }

    trim_storage_state(state)

    assert state["origins"][0]["localStorage"] == [{"name": "f", "value": "1"}]


def test_missing_origins_is_fine() -> None:
    assert trim_storage_state({"cookies": []}) == {"cookies": [], "origins": []}


def test_captcha_in_modal_is_detected() -> None:
    """hh.ru показывает капчу внутри формы отклика — она видна только по тексту."""
    page = FakePage(
        dialog_text="Пройдите капчу\nЧтобы подтвердить, что вы не робот, введите текст с картинки:",
        captcha_ticks=1,
    )

    assert detect_captcha(page)  # type: ignore[arg-type]


def test_other_dialog_is_not_captcha() -> None:
    page = FakePage(dialog_text="Отклик на вакансию\nДобавить сопроводительное", captcha_ticks=1)

    assert not detect_captcha(page)  # type: ignore[arg-type]


def test_captcha_by_url() -> None:
    assert detect_captcha(FakePage(url="https://hh.ru/captcha?x=1"))  # type: ignore[arg-type]


def test_wait_for_captcha_solved() -> None:
    """Человек решил капчу: ожидание возвращает True, как только она исчезла."""
    page = FakePage(dialog_text="Введите текст с картинки", captcha_ticks=1)

    assert wait_for_captcha_solved(page, seconds=10)  # type: ignore[arg-type]
    assert page.waits == 1


def test_wait_for_captcha_timeout() -> None:
    page = FakePage(dialog_text="Пройдите капчу", captcha_ticks=100)

    assert not wait_for_captcha_solved(page, seconds=0)  # type: ignore[arg-type]
    assert page.waits == 0
