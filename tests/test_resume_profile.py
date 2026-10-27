"""Тесты сбора профиля из резюме hh.ru: без браузера и без персональных данных."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Error as PlaywrightError

from agent.config import AppConfig, load_config
from agent.logging_setup import RunLogger
from agent.profile import ProfileError
from agent.resume import (
    RESUMES_URL,
    load_resume_profile,
    resume_block_report,
    resume_markup_report,
)

RESUME_NAME = "Java-разработчик"
RESUME_HREF = "/resume/abc123"

# Блоки фейковой страницы резюме: контейнеры hh.ru и один блок про контакты,
# который профиль читать не должен.
RESUME_BLOCKS: dict[str, list[str]] = {
    "resume-block": [
        "Желаемая должность\nJava-разработчик",
        "Опыт работы\nООО «Ромашка», ведущий разработчик, 2021-2026\n"
        "Java 21, Spring Boot, Kafka, PostgreSQL",
        "Навыки\nJava, Spring Boot, Kafka, PostgreSQL",
        "Контакты\nИван Петров, +7 (999) 123-45-67, ivan.petrov@example.com",
    ],
}


# Текст страницы резюме: шапка с именем, профессиональные разделы и контакты.
PAGE_TEXT = """Иван Петров
Java-разработчик
Желаемая должность
Java-разработчик
Опыт работы
ООО «Ромашка», ведущий разработчик, 2021-2026
Java\u00a021, Spring Boot, Kafka, PostgreSQL
Редактировать резюме
Развернуть
Показать фотографию
Портфолио
Навыки
Java, Spring Boot, Kafka, PostgreSQL
Контакты
+7 (999) 123-45-67
ivan.petrov@example.com
Образование
МГУ, прикладная математика
Подтверждение навыков
Подтверждайте навыки — это выделит вас среди других кандидатов
SQL
Docker
"""


# Опыт работы с двумя местами: у компаний есть организационно-правовая форма.
EXPERIENCE_TEXT = """Иван Петров
Опыт работы
Газпромбанк, АО
1 год и 7 месяцев
Ведущий разработчик
Июль 2024 — Январь 2026 (1 год и 7 месяцев)
Разрабатывал кредитный конвейер на Java 21 и Kafka
Ключевые навыки: Java 21, Kafka
EGAR TECHNOLOGY, INC.
8 месяцев
Ведущий разработчик
Март 2026 — сейчас (8 месяцев)
Spring Boot, PostgreSQL
"""


class FakeLocator:
    """Элемент страницы: текст, адрес ссылки и видимость."""

    def __init__(
        self,
        text: str = "",
        href: str = "",
        visible: bool = True,
        data_qa: str = "",
    ) -> None:
        self._text = text
        self._href = href
        self._data_qa = data_qa
        self.visible = visible
        self.clicked = False

    @property
    def first(self) -> "FakeLocator":
        return self

    def count(self) -> int:
        return 1 if self.visible else 0

    def is_visible(self) -> bool:
        return self.visible

    def get_attribute(self, name: str) -> str | None:
        if name == "data-qa":
            return self._data_qa
        return self._href if name == "href" else None

    def inner_text(self) -> str:
        return self._text

    def click(self) -> None:
        self.clicked = True


class FakeList:
    """Набор элементов одного селектора: count() и nth()."""

    def __init__(self, items: list[FakeLocator]) -> None:
        self._items = items

    @property
    def first(self) -> FakeLocator:
        return self._items[0] if self._items else FakeLocator(visible=False)

    def count(self) -> int:
        return len(self._items)

    def nth(self, index: int) -> FakeLocator:
        return self._items[index]


class FakePage:
    """Страница hh.ru: список резюме, открытое резюме и возможная капча."""

    def __init__(
        self,
        name: str = RESUME_NAME,
        has_resume: bool = True,
        href: str = RESUME_HREF,
        blocks: dict[str, list[str]] | None = None,
        captcha: bool = False,
        goto_error: Exception | None = None,
        page_text: str = "",
    ) -> None:
        self.url = ""
        self._name = name
        self._has_resume = has_resume
        self._href = href
        self.blocks = RESUME_BLOCKS if blocks is None else blocks
        self._captcha = captcha
        self.goto_error = goto_error
        self.page_text = page_text
        self.visited: list[str] = []

    def goto(self, url: str, **kwargs: Any) -> None:
        if self.goto_error is not None:
            raise self.goto_error
        self.url = "https://hh.ru/account/captcha" if self._captcha else url
        self.visited.append(url)

    def get_by_role(self, role: str, name: Any = None) -> FakeLocator:
        if role == "link" and isinstance(name, str) and name == self._name:
            return FakeLocator(href=self._href, visible=self._has_resume)
        return FakeLocator(visible=False)

    def get_by_text(self, text: str, exact: bool = False) -> FakeLocator:
        return FakeLocator(visible=False)

    def locator(self, selector: str) -> FakeList:
        if selector == "[data-qa]":
            items = [
                FakeLocator(text=text, data_qa=marker)
                for marker, texts in self.blocks.items()
                for text in texts
            ]
            return FakeList(items)
        marker = selector.split("'")[1] if "'" in selector else ""
        if marker == "resume" and self.page_text:
            return FakeList([FakeLocator(text=self.page_text)])
        return FakeList([FakeLocator(text=text) for text in self.blocks.get(marker, [])])

    def wait_for_load_state(self, *args: Any, **kwargs: Any) -> None:
        return None

    def wait_for_selector(self, *args: Any, **kwargs: Any) -> None:
        return None

    def wait_for_timeout(self, milliseconds: int) -> None:
        return None


def make_config(
    tmp_path: Path,
    name: str = RESUME_NAME,
    captcha_wait_seconds: int = 0,
) -> AppConfig:
    """Конфиг для тестов: название резюме, ожидание капчи и локальные пути."""
    data = {
        "search": {"queries": "Java", "area": "Москва"},
        "pipeline": {
            "limit_per_run": 1,
            "delay_between_actions": {"from": 0, "to": 0},
            "resume": {"name": name},
        },
        "browser": {"captcha_wait_seconds": captcha_wait_seconds},
    }
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    config = load_config(path)
    paths = config.data.paths.model_copy(update={"logs_dir": tmp_path / "logs"})
    return config.model_copy(update={"data": config.data.model_copy(update={"paths": paths})})


def read_events(tmp_path: Path) -> list[dict[str, Any]]:
    """Все записи журнала запуска в порядке записи."""
    events: list[dict[str, Any]] = []
    for path in sorted((tmp_path / "logs").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    return events


def test_profile_is_collected_from_resume(tmp_path: Path) -> None:
    """Профиль собирается из профессиональных блоков резюме."""
    config = make_config(tmp_path)
    page = FakePage()

    with RunLogger(tmp_path / "logs") as logger:
        profile = load_resume_profile(page, config, logger)  # type: ignore[arg-type]

    assert page.visited[0] == RESUMES_URL
    assert page.visited[1].endswith(RESUME_HREF)
    assert "Java" in profile.text
    assert "Java 21" in profile.text
    assert "Spring Boot" in profile.text
    assert "Kafka" in profile.text
    assert "ООО «Ромашка»" in profile.text


def test_profile_has_no_personal_data(tmp_path: Path) -> None:
    """Блок контактов не читается, остатки контактов вычищаются."""
    config = make_config(tmp_path)
    page = FakePage()

    with RunLogger(tmp_path / "logs") as logger:
        profile = load_resume_profile(page, config, logger)  # type: ignore[arg-type]

    assert "Иван Петров" not in profile.text
    assert "ivan.petrov@example.com" not in profile.text
    assert "+7 (999) 123-45-67" not in profile.text


def test_personal_data_is_not_logged(tmp_path: Path) -> None:
    """В журнале остаются только счётчики, а не текст резюме."""
    config = make_config(tmp_path)
    page = FakePage()

    with RunLogger(tmp_path / "logs") as logger:
        load_resume_profile(page, config, logger)  # type: ignore[arg-type]

    payload = json.dumps(read_events(tmp_path), ensure_ascii=False)
    assert "profile_loaded" in payload
    assert "Иван Петров" not in payload
    assert "ivan.petrov@example.com" not in payload
    assert "+7 (999) 123-45-67" not in payload
    assert "Kafka" not in payload


def test_profile_without_resume_name_raises(tmp_path: Path) -> None:
    """Название резюме — единственный источник профиля: без него запуск не идёт."""
    config = make_config(tmp_path, name="")
    page = FakePage()

    with RunLogger(tmp_path / "logs") as logger:
        with pytest.raises(ProfileError):
            load_resume_profile(page, config, logger)  # type: ignore[arg-type]

    assert page.visited == []


def test_profile_when_resume_not_found_raises(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    page = FakePage(has_resume=False)

    with RunLogger(tmp_path / "logs") as logger:
        with pytest.raises(ProfileError):
            load_resume_profile(page, config, logger)  # type: ignore[arg-type]

    assert any(event["event"] == "profile_resume_not_found" for event in read_events(tmp_path))


def test_profile_falls_back_to_known_selectors(tmp_path: Path) -> None:
    """Если контейнеров `resume-block` нет, работает запасной список блоков."""
    config = make_config(tmp_path)
    page = FakePage(
        blocks={
            "resume-block-experience": ["Опыт работы\nJava 21, Spring Boot, Kafka"],
        }
    )

    with RunLogger(tmp_path / "logs") as logger:
        profile = load_resume_profile(page, config, logger)  # type: ignore[arg-type]

    assert "Java 21" in profile.text
    assert "Kafka" in profile.text


def test_profile_sections_skip_personal_block(tmp_path: Path) -> None:
    """Без блоков resume-block профиль собирается по разделам страницы."""
    config = make_config(tmp_path)
    page = FakePage(
        blocks={"resume-block": ["Желаемая должность\nJava"]},
        page_text=PAGE_TEXT,
    )

    with RunLogger(tmp_path / "logs") as logger:
        profile = load_resume_profile(page, config, logger)  # type: ignore[arg-type]

    assert "Java 21" in profile.text
    assert "## Опыт работы" in profile.text
    assert "## Образование" in profile.text
    assert "- МГУ, прикладная математика" in profile.text
    assert "Подтверждение навыков" not in profile.text
    assert "Подтверждайте навыки" not in profile.text
    assert "\nSQL\n" not in profile.text
    assert "\nDocker\n" not in profile.text
    assert "МГУ" in profile.text
    assert "Иван Петров" not in profile.text
    assert "+7 (999) 123-45-67" not in profile.text
    assert "Редактировать резюме" not in profile.text
    assert "Развернуть" not in profile.text
    assert "Показать фотографию" not in profile.text
    assert "Портфолио" not in profile.text
    assert "\u00a0" not in profile.text


def test_skills_section_is_skipped(tmp_path: Path) -> None:
    """Раздел «Навыки» дублирует опыт и в профиль не попадает."""
    config = make_config(tmp_path)
    page = FakePage(
        blocks={"resume-block": ["Желаемая должность\nJava"]},
        page_text="""Иван Петров
Навыки
Java
Spring Framework
Spring MVC
Spring Security
""",
    )

    with RunLogger(tmp_path / "logs") as logger:
        profile = load_resume_profile(page, config, logger)  # type: ignore[arg-type]

    assert "## Навыки" not in profile.text
    assert "Spring MVC" not in profile.text


def test_course_year_stays_on_title_line(tmp_path: Path) -> None:
    """Год курса пишется в одной строке с названием, а не отдельной строкой."""
    config = make_config(tmp_path)
    page = FakePage(
        blocks={"resume-block": ["Желаемая должность\nJava"]},
        page_text="""Иван Петров
Повышение квалификации, курсы
Apache Kafka для разработчиков
2023
""",
    )

    with RunLogger(tmp_path / "logs") as logger:
        profile = load_resume_profile(page, config, logger)  # type: ignore[arg-type]

    assert "## Повышение квалификации, курсы" in profile.text
    assert "Apache Kafka для разработчиков, 2023" in profile.text


def test_page_noise_is_skipped(tmp_path: Path) -> None:
    """Реклама курсов и служебные подписи hh.ru в профиль не попадают."""
    config = make_config(tmp_path)
    page = FakePage(
        blocks={"resume-block": ["Желаемая должность\nJava"]},
        page_text="""Иван Петров
Опыт работы
Газпромбанк, АО
Ведущий разработчик
Май 2021 — настоящее время
Java 21, Kafka
Курсы по профессии «Инженер-программист»
Минус 5–15% на обучение — с промокодами от hh.ru
Посмотреть курсы
...
Добавьте работы в портфолио, чтобы привлечь внимание работодателей
Завершённость резюме
Пройденные тесты и экзамены
Фотографию
Средний уровень
Указать уровни
""",
    )

    with RunLogger(tmp_path / "logs") as logger:
        profile = load_resume_profile(page, config, logger)  # type: ignore[arg-type]

    for noise in (
        "Курсы по профессии",
        "промокод",
        "Посмотреть курсы",
        "Добавьте работы в портфолио",
        "Завершённость резюме",
        "Пройденные тесты",
        "Фотографию",
        "Средний уровень",
        "Указать уровни",
        "...",
    ):
        assert noise not in profile.text
    assert "Java 21" in profile.text


def test_experience_entries_get_subheadings(tmp_path: Path) -> None:
    """Каждое место работы — подзаголовок `###`, между записями пустая строка."""
    config = make_config(tmp_path)
    page = FakePage(
        blocks={"resume-block": ["Желаемая должность\nJava"]},
        page_text=EXPERIENCE_TEXT,
    )

    with RunLogger(tmp_path / "logs") as logger:
        profile = load_resume_profile(page, config, logger)  # type: ignore[arg-type]

    assert "## Опыт работы" in profile.text
    assert "### Газпромбанк, АО" in profile.text
    assert "### EGAR TECHNOLOGY, INC." in profile.text
    assert "\n\n### EGAR TECHNOLOGY, INC." in profile.text
    assert "Июль 2024 — Январь 2026\n\nРазрабатывал" in profile.text
    assert "Разрабатывал кредитный конвейер на Java 21 и Kafka\n\nКлючевые навыки" in profile.text
    assert "(8 месяцев)" not in profile.text
    assert "(1 год и 7 месяцев)" not in profile.text
    assert "8 месяцев" in profile.text
    assert "Java 21" in profile.text
    assert "Spring Boot" in profile.text


def test_education_certificates_and_courses_are_list_items(tmp_path: Path) -> None:
    """Записи образования, сертификатов и курсов идут отдельными пунктами."""
    config = make_config(tmp_path)
    page = FakePage(
        blocks={"resume-block": ["Желаемая должность\nJava"]},
        page_text="""Иван Петров
Образование
МГУ, прикладная математика
2019 · Высшее
Сертификаты
Курс Java
2023
Повышение квалификации, курсы
Курс Kafka
2023
""",
    )

    with RunLogger(tmp_path / "logs") as logger:
        profile = load_resume_profile(page, config, logger)  # type: ignore[arg-type]

    assert "## Образование\n- МГУ, прикладная математика, 2019, Высшее" in profile.text
    assert "## Сертификаты" not in profile.text
    assert "## Повышение квалификации, курсы\n- Курс Kafka, 2023" in profile.text


def test_education_record_is_one_line(tmp_path: Path) -> None:
    """Запись об образовании — одна строка, даже если записей несколько."""
    config = make_config(tmp_path)
    page = FakePage(
        blocks={"resume-block": ["Желаемая должность\nJava"]},
        page_text="""Иван Петров
Образование
Российский государственный социальный университет, Москва
Информационные системы и технологии (бакалавр)
2019 · Высшее
Другой тестовый университет, Мухосранск
Вайбкодинг и иже с ними
2087 · Поднебесное
""",
    )

    with RunLogger(tmp_path / "logs") as logger:
        profile = load_resume_profile(page, config, logger)  # type: ignore[arg-type]

    assert (
        "## Образование\n"
        "- Российский государственный социальный университет, Москва, "
        "Информационные системы и технологии (бакалавр), 2019, Высшее\n"
        "- Другой тестовый университет, Мухосранск, Вайбкодинг и иже с ними, 2087, Поднебесное"
    ) in profile.text


def test_contact_marker_lines_are_removed(tmp_path: Path) -> None:
    """Строки, где после обезличивания остались только метки, не нужны."""
    config = make_config(tmp_path)
    page = FakePage(
        blocks={"resume-block": ["Желаемая должность\nJava"]},
        page_text="""Иван Петров
О себе
Java-разработчик, микросервисы.
Почта: [почта скрыта]
+7 (999) 123-45-67
""",
    )

    with RunLogger(tmp_path / "logs") as logger:
        profile = load_resume_profile(page, config, logger)  # type: ignore[arg-type]

    assert "Java-разработчик, микросервисы." in profile.text
    assert "почта скрыта" not in profile.text
    assert "телефон скрыт" not in profile.text


def test_markup_report_lists_resume_markers() -> None:
    """Отчёт разметки показывает data-qa блоков, но не их текст."""
    report = resume_markup_report(FakePage())  # type: ignore[arg-type]

    assert any("resume-block" in line for line in report)
    assert "Желаемая должность" not in "\n".join(report)


def test_block_report_marks_personal_block(tmp_path: Path) -> None:
    """Отчёт показывает пропущенный блок контактов без текста блока."""
    config = make_config(tmp_path)
    page = FakePage()

    with RunLogger(tmp_path / "logs") as logger:
        load_resume_profile(page, config, logger)  # type: ignore[arg-type]

    report = resume_block_report(page)  # type: ignore[arg-type]

    assert len(report) == 4
    assert report[-1].endswith("пропущен (контакты или личные данные)")
    assert "Иван Петров" not in "\n".join(report)


def test_profile_without_blocks_raises(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    page = FakePage(blocks={})

    with RunLogger(tmp_path / "logs") as logger:
        with pytest.raises(ProfileError):
            load_resume_profile(page, config, logger)  # type: ignore[arg-type]

    assert any(event["event"] == "profile_empty" for event in read_events(tmp_path))


def test_profile_survives_browser_error(tmp_path: Path) -> None:
    """Ошибка браузера превращается в ProfileError: запуск прерывается с причиной."""
    config = make_config(tmp_path)
    page = FakePage(goto_error=PlaywrightError("timeout"))

    with RunLogger(tmp_path / "logs") as logger:
        with pytest.raises(ProfileError):
            load_resume_profile(page, config, logger)  # type: ignore[arg-type]


def test_profile_captcha_stops_with_error(tmp_path: Path) -> None:
    """Капчу не обходим: при нулевом ожидании профиль не собирается."""
    config = make_config(tmp_path)
    page = FakePage(captcha=True)

    with RunLogger(tmp_path / "logs") as logger:
        with pytest.raises(ProfileError):
            load_resume_profile(page, config, logger)  # type: ignore[arg-type]

    assert any(event["event"] == "profile_captcha" for event in read_events(tmp_path))

