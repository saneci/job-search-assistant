/* Логика фильтров отчёта без DOM: её проверяют тесты tests/js/filters.test.mjs.
   Скрипт подключается в браузере (window.ReportFilters) и в Node (module.exports). */
(function (root, factory) {
    'use strict';
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root) root.ReportFilters = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';

    // Значение грани «Статус» для архивных вакансий: это не статус записи,
    // а отдельный признак, поэтому у записи остаётся прежний status.
    const ARCHIVED_STATUS = 'archived';

    // Каналы, которые можно выбрать фильтром. Записи старых отчётов без поля
    // channel получают `unknown` только для вывода и в фильтр не попадают.
    const CHANNELS = new Set(['all', 'hh', 'habr', 'external']);
    const URL_KEYS = ['status', 'channel', 'q', 'date', 'dates'];

    function defaultFilters() {
        return {status: 'all', channel: 'all', query: '', files: []};
    }

    /* Канал записи: в старых отчётах поля нет — это «Без канала». */
    function channelValue(vacancy) {
        const value = vacancy && vacancy.channel;
        return value === 'hh' || value === 'habr' || value === 'external' ? value : 'unknown';
    }

    /* Приводит состояние из адреса или хранилища к допустимому виду.
       `statuses` — известные статусы отчёта, чтобы чужие значения не проходили. */
    function normalizeFilters(raw, statuses) {
        const filters = defaultFilters();
        if (!raw || typeof raw !== 'object') return filters;
        const allowed = new Set(['all'].concat(statuses || []));
        if (allowed.has(raw.status)) filters.status = raw.status;
        if (CHANNELS.has(raw.channel)) filters.channel = raw.channel;
        if (typeof raw.query === 'string') filters.query = raw.query;
        if (Array.isArray(raw.files)) {
            filters.files = raw.files.filter(name => typeof name === 'string' && name);
        }
        /* «Даты сняты» — отдельное состояние: пустой список files значит «все даты».
           Поле появляется только когда оно включено, чтобы состояние по умолчанию
           оставалось коротким. */
        if (raw.datesNone === true && filters.files.length === 0) filters.datesNone = true;
        return filters;
    }

    /* Фильтры выбраны, если задан статус, источник или текст поиска. Даты в это
       не входят по умолчанию: кнопка сброса реагирует на них только с флагом
       includeFiles, чтобы не включаться от состояния хранилища. Снятые даты —
       тоже выбор дат: отчёт пуст, и сброс должен вернуть все дни. */
    function hasSelectedFilters(filters, options) {
        const withFiles = Boolean(options && options.includeFiles === true);
        const facetsSelected = filters.status !== 'all'
            || filters.channel !== 'all'
            || filters.query !== '';
        const datesSelected = filters.files.length > 0 || filters.datesNone === true;
        return facetsSelected || (withFiles && datesSelected);
    }

    function matchesQuery(vacancy, query) {
        const text = (query || '').trim().toLowerCase();
        if (!text) return true;
        return [vacancy.title, vacancy.company, vacancy.reason, vacancy.url]
            .filter(Boolean)
            .join(' ')
            .toLowerCase()
            .includes(text);
    }

    /* Вакансия в архиве: статус у неё прежний, поэтому грань «Статус»
       смотрит на отдельный признак `archived`. Архивная запись показывается
       только в «В архиве»: иначе «Не подходит» смешивал бы живые пропуски
       и закрытые вакансии. */
    function isArchived(vacancy) {
        return Boolean(vacancy && vacancy.archived === true);
    }

    function matchesStatus(vacancy, status) {
        if (status === 'all') return true;
        if (status === ARCHIVED_STATUS) return isArchived(vacancy);
        return !isArchived(vacancy) && vacancy.status === status;
    }

    /* Итоги для карточек сводки считаются по загруженным вакансиям и без
       архивных записей: иначе закрытая вакансия с прежним статусом error
       раздувала бы карточку «Ошибки» при пустой таблице. */
    function summaryCounts(vacancies) {
        const counts = {};
        for (const vacancy of vacancies || []) {
            if (isArchived(vacancy)) continue;
            const status = vacancy && vacancy.status;
            if (!status) continue;
            counts[status] = (counts[status] || 0) + 1;
        }
        return counts;
    }

    /* Условия разных граней складываются по «И», поиск работает вместе с ними. */
    function matchesFilters(vacancy, filters) {
        if (!matchesStatus(vacancy, filters.status)) return false;
        if (filters.channel !== 'all' && channelValue(vacancy) !== filters.channel) return false;
        return matchesQuery(vacancy, filters.query);
    }

    /* Дата отмечена, если выбраны все даты или эта дата есть в списке.
       Состояние «даты сняты» гасит все отметки. */
    function isDateChecked(filters, name) {
        if (filters.datesNone) return false;
        return filters.files.length === 0 || filters.files.includes(name);
    }

    /* Переключение одной даты. Пустой список файлов значит «все даты»,
       поэтому сначала разворачиваем его в список всех известных файлов.
       Снятие последней отмеченной даты даёт «даты не выбраны», а не «все
       даты»: иначе остальные отметки проставлялись бы сами собой. */
    function toggleDate(filters, name, all) {
        const known = all || [];
        const current = filters.datesNone
            ? []
            : (filters.files.length ? filters.files.slice() : known.slice());
        const next = current.includes(name)
            ? current.filter(item => item !== name)
            : current.concat(name);
        if (next.length === 0) return {files: [], datesNone: true};
        if (known.length && next.length === known.length) return {files: [], datesNone: false};
        return {files: next, datesNone: false};
    }

    /* «Выбрать всё» работает в обе стороны: отмечены все даты — снимаем
       выделение, иначе отмечаем все. */
    function toggleAllDates(filters) {
        const everyDate = !filters.datesNone && filters.files.length === 0;
        return {files: [], datesNone: everyDate};
    }

    /* Совпадает ли выбор дат: черновик меню сравнивается с применённым
       состоянием, чтобы не перезагружать отчёт без изменений. */
    function sameDateSelection(left, right) {
        if (left.datesNone !== right.datesNone) return false;
        if (left.files.length !== right.files.length) return false;
        return left.files.every((name, index) => name === right.files[index]);
    }

    /* Состояние в адрес: пустые значения не пишем, чтобы ссылка оставалась короткой. */
    function toSearchParams(filters) {
        const params = new URLSearchParams();
        if (filters.status !== 'all') params.set('status', filters.status);
        if (filters.channel !== 'all') params.set('channel', filters.channel);
        if (filters.query) params.set('q', filters.query);
        if (filters.files.length) params.set('date', filters.files.join(','));
        if (filters.datesNone) params.set('dates', 'none');
        return params;
    }

    /* Адрес — основной носитель состояния. Если известных ключей нет,
       возвращаем null: вызывающий код возьмёт сохранённое состояние. */
    function parseSearchParams(search, statuses) {
        const params = new URLSearchParams(search || '');
        if (!URL_KEYS.some(key => params.has(key))) return null;
        const raw = {
            status: params.get('status'),
            channel: params.get('channel'),
            query: params.get('q') || '',
            files: (params.get('date') || '').split(',').map(name => name.trim())
                .filter(Boolean)
        };
        if (params.get('dates') === 'none') raw.datesNone = true;
        return normalizeFilters(raw, statuses);
    }

    return {
        CHANNELS,
        URL_KEYS,
        ARCHIVED_STATUS,
        defaultFilters,
        channelValue,
        normalizeFilters,
        hasSelectedFilters,
        isArchived,
        matchesStatus,
        summaryCounts,
        matchesQuery,
        matchesFilters,
        toSearchParams,
        parseSearchParams,
        isDateChecked,
        toggleDate,
        toggleAllDates,
        sameDateSelection
    };
});
