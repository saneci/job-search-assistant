import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';

const require = createRequire(import.meta.url);
const f = require('../../web/static/filters.js');

// Статусы отчёта: в браузере список приходит из STATUS_LABELS.
const STATUSES = ['sent', 'skipped', 'queued', 'excluded', 'error', 'rejected', 'offer'];

function vacancy(overrides = {}) {
    return {
        title: 'Java-разработчик',
        company: 'Ozon',
        reason: '',
        url: 'https://hh.ru/vacancy/1',
        status: 'sent',
        channel: 'hh',
        ...overrides
    };
}

function filters(overrides = {}) {
    return {status: 'all', channel: 'all', query: '', files: [], ...overrides};
}

test('defaultFilters: пустое состояние', () => {
    assert.deepEqual(f.defaultFilters(), {
        status: 'all',
        channel: 'all',
        query: '',
        files: []
    });
});

test('channelValue: hh, habr и записи без источника', () => {
    assert.equal(f.channelValue({channel: 'hh'}), 'hh');
    assert.equal(f.channelValue({channel: 'habr'}), 'habr');
    assert.equal(f.channelValue({}), 'unknown');
    assert.equal(f.channelValue({channel: 'superjob'}), 'unknown');
    assert.equal(f.channelValue(null), 'unknown');
});

test('hasSelectedFilters: грани включают кнопку сброса', () => {
    assert.equal(f.hasSelectedFilters(f.defaultFilters()), false);
    assert.equal(f.hasSelectedFilters(filters({status: 'sent'})), true);
    assert.equal(f.hasSelectedFilters(filters({channel: 'habr'})), true);
    assert.equal(f.hasSelectedFilters(filters({query: 'java'})), true);
});

test('hasSelectedFilters: только даты не включают кнопку', () => {
    const onlyFiles = filters({files: ['2026-10-27.json']});

    assert.equal(f.hasSelectedFilters(onlyFiles), false);
    assert.equal(f.hasSelectedFilters(onlyFiles, {includeFiles: true}), true);
});

test('hasSelectedFilters: includeFiles не отменяет грани', () => {
    const both = filters({status: 'sent', files: ['2026-10-27.json']});

    assert.equal(f.hasSelectedFilters(both, {includeFiles: true}), true);
    assert.equal(f.hasSelectedFilters(both), true);
});

test('normalizeFilters: мусор превращается в состояние по умолчанию', () => {
    assert.deepEqual(f.normalizeFilters(null), f.defaultFilters());
    assert.deepEqual(f.normalizeFilters(undefined, STATUSES), f.defaultFilters());
    assert.deepEqual(f.normalizeFilters('sent', STATUSES), f.defaultFilters());
    assert.deepEqual(f.normalizeFilters(42, STATUSES), f.defaultFilters());
});

test('normalizeFilters: неизвестный статус не проходит', () => {
    assert.equal(f.normalizeFilters({status: 'ghost'}, STATUSES).status, 'all');
});

test('normalizeFilters: известный статус сохраняется', () => {
    assert.equal(f.normalizeFilters({status: 'queued'}, STATUSES).status, 'queued');
    assert.equal(f.normalizeFilters({status: 'all'}, STATUSES).status, 'all');
});

test('normalizeFilters: канал проверяется по списку', () => {
    assert.equal(f.normalizeFilters({channel: 'habr'}, STATUSES).channel, 'habr');
    assert.equal(f.normalizeFilters({channel: 'unknown'}, STATUSES).channel, 'unknown');
    assert.equal(f.normalizeFilters({channel: 'superjob'}, STATUSES).channel, 'all');
});

test('normalizeFilters: поиск принимается только строкой', () => {
    assert.equal(f.normalizeFilters({query: 'java'}, STATUSES).query, 'java');
    assert.equal(f.normalizeFilters({query: 7}, STATUSES).query, '');
    assert.equal(f.normalizeFilters({query: null}, STATUSES).query, '');
});

test('normalizeFilters: в датах остаются только непустые строки', () => {
    const result = f.normalizeFilters(
        {files: ['2026-10-27.json', '', 5, null, '2026-10-26.json']},
        STATUSES
    );

    assert.deepEqual(result.files, ['2026-10-27.json', '2026-10-26.json']);
});

test('normalizeFilters: не массив дат игнорируется', () => {
    assert.deepEqual(f.normalizeFilters({files: '2026-10-27.json'}, STATUSES).files, []);
});

test('matchesFilters: пустые фильтры пропускают всё', () => {
    assert.equal(f.matchesFilters(vacancy(), f.defaultFilters()), true);
});

test('matchesFilters: статус сравнивается точно', () => {
    assert.equal(f.matchesFilters(vacancy({status: 'sent'}), filters({status: 'sent'})), true);
    assert.equal(f.matchesFilters(vacancy({status: 'sent'}), filters({status: 'queued'})), false);
});

test('matchesFilters: записи без канала отделяются от hh и habr', () => {
    const unknown = vacancy({channel: undefined});

    assert.equal(f.matchesFilters(unknown, filters({channel: 'unknown'})), true);
    assert.equal(f.matchesFilters(unknown, filters({channel: 'hh'})), false);
    assert.equal(f.matchesFilters(vacancy({channel: 'habr'}), filters({channel: 'habr'})), true);
});

test('matchesQuery: ищет по названию, компании, причине и ссылке', () => {
    const v = vacancy({
        title: 'Backend-разработчик',
        company: 'Сбер',
        reason: 'нет опыта с Kafka',
        url: 'https://hh.ru/vacancy/42'
    });

    assert.equal(f.matchesQuery(v, 'backend'), true);
    assert.equal(f.matchesQuery(v, 'сбер'), true);
    assert.equal(f.matchesQuery(v, 'kafka'), true);
    assert.equal(f.matchesQuery(v, 'vacancy/42'), true);
    assert.equal(f.matchesQuery(v, 'python'), false);
});

test('matchesQuery: регистр и пробелы по краям не мешают', () => {
    assert.equal(f.matchesQuery(vacancy({company: 'Ozon'}), '  OZON  '), true);
    assert.equal(f.matchesQuery(vacancy(), ''), true);
    assert.equal(f.matchesQuery(vacancy(), '   '), true);
});

test('matchesFilters: поиск складывается с гранью', () => {
    const v = vacancy({title: 'Java-разработчик', status: 'sent', channel: 'hh'});
    const selected = filters({status: 'sent', channel: 'hh', query: 'java'});

    assert.equal(f.matchesFilters(v, selected), true);
    assert.equal(f.matchesFilters(v, {...selected, query: 'python'}), false);
    assert.equal(f.matchesFilters(v, {...selected, channel: 'habr'}), false);
});

test('toSearchParams: пустое состояние не оставляет параметров', () => {
    assert.equal(f.toSearchParams(f.defaultFilters()).toString(), '');
});

test('toSearchParams: каждая грань попадает в адрес', () => {
    assert.equal(f.toSearchParams(filters({status: 'sent'})).get('status'), 'sent');
    assert.equal(f.toSearchParams(filters({channel: 'habr'})).get('channel'), 'habr');
    assert.equal(f.toSearchParams(filters({query: 'java'})).get('q'), 'java');
    assert.equal(
        f.toSearchParams(filters({files: ['a.json', 'b.json']})).get('date'),
        'a.json,b.json'
    );
});

test('toSearchParams: полное состояние читается обратно', () => {
    const source = filters({
        status: 'error',
        channel: 'unknown',
        query: 'java',
        files: ['2026-10-27.json']
    });
    const params = f.toSearchParams(source);

    assert.equal(params.get('status'), 'error');
    assert.equal(params.get('channel'), 'unknown');
    assert.equal(params.get('q'), 'java');
    assert.equal(params.get('date'), '2026-10-27.json');
});

test('parseSearchParams: пустой адрес даёт null', () => {
    assert.equal(f.parseSearchParams('', STATUSES), null);
    assert.equal(f.parseSearchParams('?page=2', STATUSES), null);
});

test('parseSearchParams: известный ключ включает состояние', () => {
    const result = f.parseSearchParams('?status=sent', STATUSES);

    assert.notEqual(result, null);
    assert.equal(result.status, 'sent');
    assert.equal(result.channel, 'all');
});

test('parseSearchParams: даты разбираются через запятую', () => {
    const result = f.parseSearchParams('?date=2026-10-27.json,2026-10-26.json', STATUSES);

    assert.deepEqual(result.files, ['2026-10-27.json', '2026-10-26.json']);
});

test('parseSearchParams: даты с пробелами и пустыми кусками чистятся', () => {
    const result = f.parseSearchParams('?date=%202026-10-27.json%20,,', STATUSES);

    assert.deepEqual(result.files, ['2026-10-27.json']);
});

test('parseSearchParams: неизвестные значения заменяются на all', () => {
    const result = f.parseSearchParams('?status=ghost&channel=superjob', STATUSES);

    assert.equal(result.status, 'all');
    assert.equal(result.channel, 'all');
});

test('полный круг: адрес -> состояние -> адрес', () => {
    const search = '?status=queued&channel=habr&q=java&date=2026-10-27.json';
    const restored = f.parseSearchParams(search, STATUSES);
    const again = f.toSearchParams(restored).toString();

    assert.equal(again, 'status=queued&channel=habr&q=java&date=2026-10-27.json');
});

test('регресс: даты сохраняются, но кнопку сброса не включают', () => {
    const restored = f.parseSearchParams('?date=2026-10-27.json', STATUSES);

    assert.deepEqual(restored.files, ['2026-10-27.json']);
    assert.equal(f.hasSelectedFilters(restored), false);
    assert.equal(f.hasSelectedFilters(restored, {includeFiles: true}), true);
});

