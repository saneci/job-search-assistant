/* Логика фильтров отчёта без DOM: её проверяют тесты tests/js/filters.test.mjs.
   Скрипт подключается в браузере (window.ReportFilters) и в Node (module.exports). */
(function (root, factory) {
    'use strict';
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root) root.ReportFilters = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';

    // Каналы записи. `unknown` — записи старых отчётов без поля channel.
    const CHANNELS = new Set(['all', 'hh', 'habr', 'external', 'unknown']);
    const URL_KEYS = ['status', 'channel', 'q', 'date'];

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
        return filters;
    }

    /* Фильтры выбраны, если задан статус, источник или текст поиска. Даты в это
       не входят: кнопка сброса на них не реагирует, но в сохранённом состоянии
       они остаются, поэтому у записи в хранилище свой флаг includeFiles. */
    function hasSelectedFilters(filters, options) {
        const withFiles = Boolean(options && options.includeFiles === true);
        const facetsSelected = filters.status !== 'all'
            || filters.channel !== 'all'
            || filters.query !== '';
        return facetsSelected || (withFiles && filters.files.length > 0);
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

    /* Условия разных граней складываются по «И», поиск работает вместе с ними. */
    function matchesFilters(vacancy, filters) {
        if (filters.status !== 'all' && vacancy.status !== filters.status) return false;
        if (filters.channel !== 'all' && channelValue(vacancy) !== filters.channel) return false;
        return matchesQuery(vacancy, filters.query);
    }

    /* Состояние в адрес: пустые значения не пишем, чтобы ссылка оставалась короткой. */
    function toSearchParams(filters) {
        const params = new URLSearchParams();
        if (filters.status !== 'all') params.set('status', filters.status);
        if (filters.channel !== 'all') params.set('channel', filters.channel);
        if (filters.query) params.set('q', filters.query);
        if (filters.files.length) params.set('date', filters.files.join(','));
        return params;
    }

    /* Адрес — основной носитель состояния. Если известных ключей нет,
       возвращаем null: вызывающий код возьмёт сохранённое состояние. */
    function parseSearchParams(search, statuses) {
        const params = new URLSearchParams(search || '');
        if (!URL_KEYS.some(key => params.has(key))) return null;
        return normalizeFilters({
            status: params.get('status'),
            channel: params.get('channel'),
            query: params.get('q') || '',
            files: (params.get('date') || '').split(',').map(name => name.trim())
                .filter(Boolean)
        }, statuses);
    }

    return {
        CHANNELS,
        URL_KEYS,
        defaultFilters,
        channelValue,
        normalizeFilters,
        hasSelectedFilters,
        matchesQuery,
        matchesFilters,
        toSearchParams,
        parseSearchParams
    };
});
