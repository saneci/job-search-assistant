"""Резюме hh.ru: профиль для LLM и подъём в поиске.

Профиль собирается из профессиональных блоков резюме, выбранного по названию
`pipeline.resume.name`, и живёт только в памяти процесса: персональные данные
не читаются. Исключение — отладочный `--dump-profile`: обезличенный профиль
по явному запросу пишется в `data/`. Подъём в поиске выполняется не чаще, чем
раз в `resume.boost_interval_hours`, и отключается флагом `resume.boost`.

Кэш (`paths.cache`) — плоский JSON ``{название резюме: время последнего подъёма}``.
Любая проблема с подъёмом не останавливает запуск: агент пишет событие в лог
и переходит к основному сценарию.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from .browser import NAVIGATION_TIMEOUT_MS, detect_captcha, wait_for_captcha_solved
from .config import AppConfig
from .logging_setup import RunLogger
from .profile import (
    Profile,
    ProfileError,
    drop_contact_markers,
    normalize_text,
    scrub_personal_data,
)

RESUMES_URL = "https://hh.ru/applicant/resumes"
BOOST_RE = re.compile(r"поднять в поиске", re.IGNORECASE)
BOOST_SELECTORS = (
    "[data-qa='resume-update-button']",
    "[data-qa='resume-boost-button']",
    "button[data-qa*='resume-update']",
    "[data-qa='resume-update-button'] button",
)
BOOST_RESULT_WAIT_MS = 2000

# Блок резюме hh.ru: внутри лежат должность, опыт, образование и навыки.
RESUME_BLOCK_SELECTOR = "[data-qa='resume-block']"
# Сколько ждать появления блоков резюме: hh.ru дорисовывает их скриптом.
PROFILE_WAIT_MS = 15000
# Заголовки блоков про контакты и личные сведения: такие блоки не читаем.
RESUME_PERSONAL_BLOCK_RE = re.compile(
    r"контакт|телефон|почт|e-?mail|фото|личн|дата рождения|гражданств|"
    r"переезд|адрес|соцсет|мессенджер|рекомендател",
    re.IGNORECASE,
)

# Запасной список блоков на случай другой вёрстки, если контейнеров нет.
# Здесь только профессиональная часть — должность, опыт, образование, навыки,
# языки, сертификаты, проекты. Контакты и личные сведения не входят намеренно.
RESUME_PROFILE_SELECTORS = (
    "[data-qa='resume-block-title-position']",
    "[data-qa='resume-block-position']",
    "[data-qa='resume-block-experience']",
    "[data-qa='resume-block-education']",
    "[data-qa='resume-block-skills']",
    "[data-qa='resume-block-about']",
    "[data-qa='resume-block-languages']",
    "[data-qa='resume-block-certificates']",
    "[data-qa='resume-block-projects']",
    "[data-qa='resume-block-additional']",
)
# Сколько элементов одного блока читаем: защита от бесконечного резюме.
RESUME_BLOCK_ITEMS_LIMIT = 20

# Контейнер страницы резюме: с него читаем текст, если блоки не нашлись.
RESUME_PAGE_SELECTORS = ("[data-qa='resume']", "main", "[role='main']")

# Заголовки профессиональных разделов резюме: по ним нарезаем текст страницы.
PROFILE_SECTION_TITLES = (
    "желаемая должность",
    "опыт работы",
    "образование",
    "навыки",
    "о себе",
    "языки",
    "сертификаты",
    "проекты",
    "дополнительная информация",
    "повышение квалификации",
    "курсы",
)
# Рекламные и сервисные блоки hh.ru: их содержимое в профиль не попадает.
SKIP_SECTION_TITLES = (
    "курсы по профессии",
    "завершённость резюме",
    "завершенность резюме",
    "пройденные тесты",
    "портфолио",
    "подтверждение навыков",
    "сертификаты",
    "навыки",
)
# Заголовки разделов про личные данные: их содержимое в профиль не попадает.
PERSONAL_SECTION_TITLES = (
    "контакты",
    "контактная информация",
    "личная информация",
    "персональные данные",
    "фото",
    "рекомендации",
    "переезд",
    "гражданство",
    "документы",
)
# Всё, что в профиль не читаем: личные данные и рекламные блоки hh.ru.
SKIPPED_SECTION_TITLES = PERSONAL_SECTION_TITLES + SKIP_SECTION_TITLES
# Меньше этого объёма считаем сбор по блокам неполным и пробуем разделы.
PROFILE_MIN_CHARS = 300
# Организационно-правовая форма в названии компании: по ней видно начало
# новой записи об опыте работы.
_COMPANY_FORMS = r"(?:АО|ООО|ПАО|ЗАО|ОАО|ИП|НКО)"
_COMPANY_FORMS_EN = r"(?:Inc|LLC|Ltd|GmbH|Corp|Company|Group)"
EXPERIENCE_COMPANY_RE = re.compile(
    rf"^{_COMPANY_FORMS}\b"
    rf"|(?:,\s*)?{_COMPANY_FORMS}\s*$"
    rf"|\b{_COMPANY_FORMS_EN}\.?\s*$",
    re.IGNORECASE,
)

# Год курса отдельной строкой: в профиле приклеиваем его к названию курса.
_COURSE_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
# Организация и год в конце строки курса: записи разделяются по ней.
_COURSE_END_RE = re.compile(r",\s*(?:19|20)\d{2}\s*$")
# Период работы в записи об опыте: «Март 2026 — сейчас (8 месяцев)».
_PERIOD_RE = re.compile(
    r"(?:[а-яё]+\s+)?\d{4}\s*[—–-]\s*(?:(?:[а-яё]+\s+)?\d{4}|сейчас|настоящее время)",
    re.IGNORECASE,
)
# Длительность в скобках после периода: «(8 месяцев)», «(1 год и 7 месяцев)».
_PERIOD_DURATION_RE = re.compile(
    r"\s*\(\s*\d+\s*(?:год|года|лет|месяц|месяца|месяцев)[^)]*\)\s*$",
    re.IGNORECASE,
)
# Строка с перечнем навыков в записи об опыте.
_SKILLS_LINE_RE = re.compile(r"^Ключевые навыки\s*:", re.IGNORECASE)
# Год в строке образования закрывает запись.
_EDUCATION_END_RE = re.compile(r"\b(?:19|20)\d{2}\b")

# Приглашения и реклама hh.ru, которые попадают в текст страницы.
RESUME_NOISE_RE = re.compile(
    r"промокод|посмотреть курсы|добавьте работы в портфолио|"
    r"чтобы привлечь внимание работодателей|на обучение|"
    r"завершённость резюме|завершенность резюме|пройденные тесты|"
    r"подтверждайте навыки|указать уровни|средний уровень|по-русски",
    re.IGNORECASE,
)
# Кнопки и служебные подписи страницы: в профиль они не нужны.
RESUME_UI_LINE_RE = re.compile(
    r"^(редактировать|скачать|распечатать|поднять в поиске|копировать|"
    r"настройки|создать резюме|показать|скрыть|развернуть|свернуть|"
    r"ещё|вернуться|фотографи|фото|портфолио|добавить|изменить|обновить|"
    r"подробнее|перейти|откликнуться|сохранить|удалить|дублировать)\b",
    re.IGNORECASE,
)

STATUS_BOOSTED = "boosted"
STATUS_FRESH = "fresh"
STATUS_DISABLED = "disabled"
STATUS_UNAVAILABLE = "unavailable"


class ResumeCache:
    """Кэш подъёмов резюме: когда каждое резюме поднимали в последний раз."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._items: dict[str, str] = {}

    def load(self) -> dict[str, str]:
        """Читает кэш; битый или отсутствующий файл считается пустым."""
        if not self.path.exists():
            self._items = {}
            return self._items
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._items = {}
            return self._items
        if not isinstance(raw, dict):
            self._items = {}
            return self._items
        self._items = {str(key): str(value) for key, value in raw.items()}
        return self._items

    def last_boost(self, name: str) -> datetime | None:
        """Время последнего подъёма резюме или None (в кэше нет отметки)."""
        value = self.load().get(name)
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def due(self, name: str, interval_hours: int, now: datetime | None = None) -> bool:
        """Пора ли поднимать резюме: прошло не меньше interval_hours с прошлого раза."""
        last = self.last_boost(name)
        if last is None:
            return True
        moment = now or datetime.now().astimezone()
        if last.tzinfo is None:
            last = last.astimezone()
        return moment - last >= timedelta(hours=interval_hours)

    def mark(self, name: str, moment: datetime | None = None) -> datetime:
        """Запоминает время подъёма резюме и сохраняет кэш."""
        at = moment or datetime.now().astimezone()
        items = self.load()
        items[name] = at.isoformat(timespec="seconds")
        self._items = items
        self._save()
        return at

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(self._items, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.path)


def boost_resume(page: Page, config: AppConfig, logger: RunLogger) -> str:
    """Поднимает резюме в поиске, если пришло время. Возвращает статус операции."""
    if not config.pipeline.resume.boost:
        logger.event("resume_boost_disabled", reason="boost: false")
        return STATUS_DISABLED

    name = config.pipeline.resume.name.strip()
    if not name:
        logger.event("resume_boost_disabled", reason="pipeline.resume.name не задан")
        return STATUS_DISABLED

    cache = ResumeCache(config.data.paths.cache)
    interval = config.pipeline.resume.boost_interval_hours
    last = cache.last_boost(name)
    if last is not None and not cache.due(name, interval):
        logger.event("resume_boost_fresh", resume=name, last_boost=last.isoformat())
        logger.info(
            f"Резюме «{name}» поднимали {last:%Y-%m-%d %H:%M}, "
            f"следующий подъём не раньше чем через {interval} ч."
        )
        return STATUS_FRESH

    try:
        page.goto(RESUMES_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        link = _find_resume_link(page, name)
        if link is None:
            logger.event("resume_not_found", resume=name, url=page.url)
            logger.info(f"Не нашёл резюме «{name}» в списке резюме, подъём пропущен.")
            return STATUS_UNAVAILABLE

        link.click()
        page.wait_for_load_state("domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)

        button = _find_boost_button(page)
        if button is None or not _is_enabled(button):
            logger.event("resume_boost_unavailable", resume=name, url=page.url)
            logger.info("Кнопки «Поднять в поиске» нет, подъём не требуется.")
            return STATUS_UNAVAILABLE

        button.click()
        page.wait_for_timeout(BOOST_RESULT_WAIT_MS)
    except PlaywrightError as exc:
        logger.event("resume_boost_error", resume=name, error=str(exc))
        logger.info(f"Не удалось поднять резюме в поиске: {exc}")
        return STATUS_UNAVAILABLE

    try:
        at = cache.mark(name)
    except OSError as exc:
        logger.event("resume_cache_error", resume=name, error=str(exc))
        logger.info(f"Резюме поднято, но отметка в кэше не сохранена: {exc}")
        return STATUS_BOOSTED

    logger.event("resume_boosted", resume=name, boosted_at=at.isoformat())
    logger.info(f"Резюме «{name}» поднято в поиске.")
    return STATUS_BOOSTED


def _find_resume_link(page: Page, name: str) -> Locator | None:
    """Ссылка на резюме с указанным названием в списке резюме."""
    for locator in _candidates(page, name):
        if _is_visible(locator):
            return locator
    return None


def _candidates(page: Page, name: str) -> list[Locator]:
    candidates: list[Locator] = []
    try:
        candidates.append(page.get_by_role("link", name=name).first)
    except PlaywrightError:
        pass
    try:
        candidates.append(page.get_by_text(name, exact=True).first)
    except PlaywrightError:
        pass
    return candidates


def _find_boost_button(page: Page) -> Locator | None:
    """Кнопка «Поднять в поиске» на странице резюме."""
    for selector in BOOST_SELECTORS:
        locator = page.locator(selector).first
        if _is_visible(locator):
            return locator
    for role in ("button", "link"):
        try:
            locator = page.get_by_role(role, name=BOOST_RE).first
        except PlaywrightError:
            continue
        if _is_visible(locator):
            return locator
    return None


def _is_visible(locator: Locator) -> bool:
    try:
        return locator.count() > 0 and locator.is_visible()
    except PlaywrightError:
        return False


def _is_enabled(locator: Locator) -> bool:
    try:
        return locator.is_enabled()
    except PlaywrightError:
        return False


# --- Профиль из резюме ------------------------------------------------------


def load_resume_profile(page: Page, config: AppConfig, logger: RunLogger) -> Profile:
    """Собирает профиль из резюме по названию `pipeline.resume.name`.

    Читаются только профессиональные блоки резюме. Персональные данные не
    извлекаются, а контакты, попавшие в текст, вычищает `scrub_personal_data`.
    Профиль живёт в памяти процесса: в журнал уходят только счётчики.
    """
    name = config.pipeline.resume.name.strip()
    if not name:
        raise ProfileError(
            "Не задано pipeline.resume.name: это название резюме — источника профиля"
        )

    _open_resume(page, config, logger, name)
    text, blocks = collect_resume_text(page)
    if not text:
        logger.event("profile_empty", resume=name, url=page.url)
        raise ProfileError(f"Резюме «{name}» пустое: профиль собрать не удалось")

    logger.event("profile_loaded", resume=name, chars=len(text), blocks=blocks)
    logger.info(f"Профиль собран из резюме «{name}»: {len(text)} символов.")
    return Profile(text=text)


def collect_resume_text(page: Page) -> tuple[str, int]:
    """Профессиональный текст резюме и число непустых блоков.

    Основной источник — блоки-контейнеры `[data-qa='resume-block']`, внутри
    которых у hh.ru лежат должность, опыт, образование и навыки. Блоки про
    контакты и личные сведения отбрасываются до чтения, остатки контактов
    вычищает `scrub_personal_data`. Если контейнеров на странице нет, работает
    запасной список `RESUME_PROFILE_SELECTORS`.
    """
    parts, seen = _collect_blocks(page, RESUME_BLOCK_SELECTOR)
    text = "\n\n".join(parts)

    # Вёрстка hh.ru меняется: если блоков нет или их мало, читаем разделы
    # страницы по заголовкам — это не зависит от data-qa.
    if len(text) < PROFILE_MIN_CHARS:
        sections = collect_resume_sections(page)
        if len(sections) > len(text):
            text = sections
            parts = [sections]

    if not text.strip():
        for selector in RESUME_PROFILE_SELECTORS:
            for block in _visible_texts(page, selector):
                _append_part(parts, seen, block)
        text = "\n\n".join(parts)

    return text, len(parts)


def collect_resume_sections(page: Page) -> str:
    """Профессиональные разделы резюме из текста страницы, без data-qa.

    Резервный способ на случай незнакомой вёрстки: текст страницы режется по
    заголовкам разделов. Всё до первого заголовка отбрасывается — там шапка с
    именем. Содержимое личных разделов в профиль не попадает.
    """
    text = _first_visible_text(page, RESUME_PAGE_SELECTORS)
    if not text:
        return ""

    sections: list[str] = []
    title = ""
    lines: list[str] = []
    in_personal = False

    for line in text.splitlines():
        if _is_section_title(line, SKIPPED_SECTION_TITLES):
            _add_section(sections, title, lines)
            title, lines = "", []
            in_personal = True
            continue
        if _is_section_title(line, PROFILE_SECTION_TITLES):
            _add_section(sections, title, lines)
            title, lines = line, []
            in_personal = False
            continue
        if in_personal or not title or _is_noise_line(line):
            continue
        lines.append(line)

    _add_section(sections, title, lines)
    return drop_contact_markers(scrub_personal_data("\n\n".join(sections))).strip()


def _add_section(sections: list[str], title: str, lines: list[str]) -> None:
    """Добавляет раздел с заголовком: LLM проще читать структуру, чем поток."""
    if not title or not lines:
        return
    lower = title.lower()
    if lower.startswith("опыт работы"):
        sections.append(f"## {title}\n{_format_experience(lines)}")
        return
    if "курс" in lower:
        sections.append(f"## {title}\n{_format_list(_format_courses(lines))}")
        return
    if lower.startswith("образование"):
        sections.append(f"## {title}\n{_format_list(_format_education(lines))}")
        return
    sections.append(f"## {title}\n" + "\n".join(lines))


def _format_list(items: list[str]) -> str:
    """Каждая запись — отдельным пунктом списка: в Markdown они не сливаются."""
    return "\n".join(f"- {item}" for item in items)


def _format_education(lines: list[str]) -> list[str]:
    """Запись об образовании — одна строка: вуз, город, специальность, год, уровень."""
    records: list[str] = []
    current: list[str] = []
    for line in lines:
        current.append(line.replace(" · ", ", "))
        if _EDUCATION_END_RE.search(line):
            records.append(", ".join(current))
            current = []
    if current:
        records.append(", ".join(current))
    return records


def _format_courses(lines: list[str]) -> list[str]:
    """Курс — одна строка: название, организация, год."""
    records: list[str] = []
    current: list[str] = []
    for line in lines:
        current.append(line)
        if _COURSE_YEAR_RE.match(line) or _COURSE_END_RE.search(line):
            records.append(", ".join(current))
            current = []
    if current:
        records.append(", ".join(current))
    return records


def _is_noise_line(line: str) -> bool:
    """Служебная строка страницы: подпись, кнопка, реклама или многоточие."""
    if not re.search(r"\w", line):
        return True
    return bool(RESUME_UI_LINE_RE.match(line) or RESUME_NOISE_RE.search(line))


def _format_experience(lines: list[str]) -> str:
    """Делит опыт на записи: компания — подзаголовок `###`, между записями пусто.

    Начало записи — первая строка раздела и каждая строка с организационно-
    правовой формой («Газпромбанк, АО», «EGAR TECHNOLOGY, INC.»).
    """
    entries: list[list[str]] = []
    for index, line in enumerate(lines):
        if index == 0 or EXPERIENCE_COMPANY_RE.search(line):
            entries.append([line])
        else:
            entries[-1].append(line)

    blocks: list[str] = []
    for entry in entries:
        head, *rest = entry
        body = _with_skills_gap(_with_period_gap(rest))
        blocks.append("\n".join([f"### {head}", *body]).rstrip())
    return "\n\n".join(blocks)


def _with_period_gap(lines: list[str]) -> list[str]:
    """После периода — пустая строка, длительность в скобках не дублируется."""
    result: list[str] = []
    for line in lines:
        if _PERIOD_RE.search(line):
            result.append(_PERIOD_DURATION_RE.sub("", line))
            result.append("")
            continue
        result.append(line)
    return result


def _with_skills_gap(lines: list[str]) -> list[str]:
    """Перед «Ключевые навыки» — пустая строка: список не сливается с текстом."""
    result: list[str] = []
    for line in lines:
        if _SKILLS_LINE_RE.match(line) and result and result[-1]:
            result.append("")
        result.append(line)
    return result


def _is_section_title(line: str, titles: tuple[str, ...]) -> bool:
    """Строка является заголовком раздела (регистр и хвост вроде «6 лет» не важны)."""
    normalized = line.lower().strip(" :»«")
    return any(
        normalized == title
        or normalized.startswith(f"{title}:")
        or normalized.startswith(f"{title} ")
        or normalized.startswith(f"{title},")
        for title in titles
    )


def _first_visible_text(page: Page, selectors: tuple[str, ...]) -> str:
    """Текст первого видимого контейнера из списка селекторов."""
    for selector in selectors:
        try:
            locator = page.locator(selector).first
        except PlaywrightError:
            continue
        if not _is_visible(locator):
            continue
        try:
            text = locator.inner_text().strip()
        except PlaywrightError:
            continue
        if text:
            return normalize_text(text)
    return ""


def resume_markup_report(page: Page, limit: int = 40) -> list[str]:
    """Какие `data-qa` есть на странице резюме и сколько в них текста.

    Нужен, когда профиль собирается неполным: по отчёту видно, за какие
    селекторы цепляться. Текст блоков не печатается, только имена и длины.
    """
    try:
        locator = page.locator("[data-qa]")
        total = min(locator.count(), 300)
    except PlaywrightError:
        return []

    report: list[str] = []
    seen: set[str] = set()
    for index in range(total):
        item = locator.nth(index)
        marker = _data_qa(item)
        if not marker or "resume" not in marker.lower() or marker in seen:
            continue
        seen.add(marker)
        if not _is_visible(item):
            continue
        try:
            length = len(item.inner_text())
        except PlaywrightError:
            length = 0
        report.append(f"- {marker}: {length} символов")
        if len(report) >= limit:
            break
    return report


def _data_qa(locator: Locator) -> str:
    """Значение атрибута `data-qa` элемента (пусто, если атрибута нет)."""
    try:
        return (locator.get_attribute("data-qa") or "").strip()
    except PlaywrightError:
        return ""


def is_personal_block(text: str) -> bool:
    """Блок резюме про контакты и личные сведения: читать его нельзя.

    Проверяется заголовок блока — первая строка. Так блок «Опыт работы» не
    отбрасывается из-за слова «почта» или «адрес» внутри описания.
    """
    lines = text.splitlines()
    head = " ".join(lines[0].split()) if lines else ""
    return bool(RESUME_PERSONAL_BLOCK_RE.search(head[:80]))


def resume_block_report(page: Page) -> list[str]:
    """Отчёт по блокам резюме для отладки: без текста, только размеры.

    Нужен, когда профиль собрался неполным: видно, сколько блоков нашлось
    и какие из них пропущены как личные данные.
    """
    report: list[str] = []
    for index, text in enumerate(_visible_texts(page, RESUME_BLOCK_SELECTOR), start=1):
        if is_personal_block(text):
            report.append(f"- блок {index}: пропущен (контакты или личные данные)")
        else:
            report.append(f"- блок {index}: {len(text)} символов")
    return report


def _collect_blocks(page: Page, selector: str) -> tuple[list[str], set[str]]:
    """Тексты видимых блоков резюме без блоков про личные данные."""
    parts: list[str] = []
    seen: set[str] = set()
    for text in _visible_texts(page, selector):
        if is_personal_block(text):
            continue
        _append_part(parts, seen, text)
    return parts, seen


def _append_part(parts: list[str], seen: set[str], text: str) -> None:
    """Добавляет обезличенный текст блока, если он ещё не встречался."""
    cleaned = drop_contact_markers(scrub_personal_data(text)).strip()
    if not cleaned or cleaned in seen:
        return
    seen.add(cleaned)
    parts.append(cleaned)


def _visible_texts(page: Page, selector: str) -> list[str]:
    """Тексты видимых элементов по селектору, не больше лимита на блок."""
    try:
        locator = page.locator(selector)
        total = min(locator.count(), RESUME_BLOCK_ITEMS_LIMIT)
    except PlaywrightError:
        return []

    texts: list[str] = []
    for index in range(total):
        item = locator.nth(index)
        if not _is_visible(item):
            continue
        try:
            text = normalize_text(item.inner_text())
        except PlaywrightError:
            continue
        if text:
            texts.append(text)
    return texts


def _open_resume(page: Page, config: AppConfig, logger: RunLogger, name: str) -> None:
    """Открывает страницу резюме с указанным названием."""
    try:
        page.goto(RESUMES_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        _ensure_no_captcha(page, config, logger)

        link = _find_resume_link(page, name)
        if link is None:
            logger.event("profile_resume_not_found", resume=name, url=page.url)
            raise ProfileError(f"Резюме «{name}» не найдено в списке резюме hh.ru")

        href = _link_href(link)
        if href:
            page.goto(
                urljoin(RESUMES_URL, href),
                wait_until="domcontentloaded",
                timeout=NAVIGATION_TIMEOUT_MS,
            )
        else:
            link.click()
        page.wait_for_load_state("domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        _ensure_no_captcha(page, config, logger)
        # Блоки резюме могут появиться позже: ждём их, но не считаем это ошибкой,
        # если их нет — профиль всё равно пытаемся собрать.
        try:
            page.wait_for_selector(RESUME_BLOCK_SELECTOR, timeout=PROFILE_WAIT_MS)
        except PlaywrightError:
            pass
    except PlaywrightError as exc:
        raise ProfileError(f"Не удалось открыть резюме «{name}»: {exc}") from exc


def _ensure_no_captcha(page: Page, config: AppConfig, logger: RunLogger) -> None:
    """Капчу не обходим: ждём ручного решения, иначе профиль не собрать."""
    if not detect_captcha(page):
        return

    logger.event("profile_captcha", url=page.url)
    logger.info(
        "hh.ru просит пройти капчу: решите её в окне браузера, ждём до "
        f"{config.browser.captcha_wait_seconds} с."
    )
    if not wait_for_captcha_solved(page, config.browser.captcha_wait_seconds):
        raise ProfileError("hh.ru просит пройти капчу: профиль из резюме не получен")


def _link_href(locator: Locator) -> str:
    """Адрес резюме из ссылки списка резюме (пусто, если атрибута нет)."""
    try:
        return (locator.get_attribute("href") or "").strip()
    except PlaywrightError:
        return ""
