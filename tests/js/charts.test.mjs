/* Тест вёрстки графиков страницы метрик. Шаблон рендерит сервер, поэтому тест
   читает разметку Jinja и инлайн-стили и фиксирует внешний вид полос, подписей
   сегментов и легенд. Браузер не запускается. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const TEMPLATE = join(ROOT, 'web', 'templates', 'metrics.html');
const BASE_CSS = join(ROOT, 'web', 'static', 'base.css');
const METRICS_CSS = join(ROOT, 'web', 'static', 'metrics.css');
const html = readFileSync(TEMPLATE, 'utf8');
// Стили разбиты на модули: общий base.css и страничный metrics.css.
const css = readFileSync(BASE_CSS, 'utf8') + '\n' + readFileSync(METRICS_CSS, 'utf8');
// Каждый график начинается с <div class="chart-section">, поэтому делим страницу
// по этим блокам: так проверка одного графика не задевает соседние.
const sections = html.split('<div class="chart-section">').slice(1);

const DISTRIBUTION = 'Распределение обработанных вакансий';
const FATE = 'Судьба отправленных откликов';
const CHANNELS = 'Каналы поиска';
const CHARTS = [DISTRIBUTION, FATE, CHANNELS];

/* Многострочная разметка сравнивается как одна строка: переносы и отступы
   в шаблоне не должны ломать проверки. */
function flat(text) {
    return text.replace(/\s+/g, ' ').trim();
}

function section(title) {
    const found = sections.find(item => flat(item).includes(`<h2>${title}`));
    assert.ok(found, `на странице метрик нет графика «${title}»`);
    return flat(found);
}

/* Первый сегмент полосы: его тег с атрибутами и содержимое подписи. */
function segment(title) {
    const block = section(title);
    const match = block.match(/<div class="stack-segment"[^>]*>(.*?)<\/div>/);
    assert.ok(match, `в графике «${title}» нет сегмента полосы`);
    return {tag: match[0], inner: flat(match[1])};
}

function rule(selector) {
    const index = css.indexOf(`${selector} {`);
    assert.notEqual(index, -1, `нет CSS-правила ${selector}`);
    return flat(css.slice(index, css.indexOf('}', index)));
}

function assertRule(selector, declarations) {
    const body = rule(selector);
    for (const declaration of declarations) {
        assert.ok(body.includes(declaration), `${selector}: в «${body}» нет «${declaration}»`);
    }
}

test('полоса графика: ширина по проценту, цвет по статусу, подсказка с числом', () => {
    for (const title of CHARTS) {
        const {tag} = segment(title);

        assert.ok(tag.includes('style="width: {{ item.percent }}%; background: {{ item.color }};"'),
            `график «${title}»: сегмент должен занимать долю item.percent и красить item.color`);
        assert.ok(tag.includes('title="{{ item.label }}: {{ item.count }}"'),
            `график «${title}»: в подсказке должны быть название и число`);
    }
});

test('нулевые сегменты не рисуются: полоса без пустых стопов', () => {
    assert.ok(section(DISTRIBUTION).includes('{% for item in metrics.distribution if item.count %}'),
        'нулевой статус в «Распределении» не должен занимать место на полосе');
    assert.ok(section(FATE).includes('{% for item in metrics.fate if item.count %}'),
        'нулевой статус в «Судьбе откликов» не должен занимать место на полосе');
});

test('подписи сегментов — только проценты, без названий статусов', () => {
    const thresholds = {[DISTRIBUTION]: 7, [FATE]: 10, [CHANNELS]: 10};

    for (const [title, threshold] of Object.entries(thresholds)) {
        const {inner} = segment(title);

        assert.equal(inner,
            `{% if item.percent >= ${threshold} %}<span>{{ item.percent_text }}</span>{% endif %}`,
            `график «${title}»: подпись сегмента — процент, порог ${threshold}%`);
        assert.ok(!inner.includes('item.label'),
            `график «${title}»: название статуса на полосе не выводится`);
    }
});

test('судьба откликов: процент на полосе, название и число — в легенде', () => {
    const block = section(FATE);

    assert.ok(block.includes('{% if metrics.has_responses %}'),
        'пустой отчёт показывает подсказку вместо полосы');
    assert.ok(block.includes('Отправленных откликов пока нет.'),
        'нет пояснения для пустого отчёта');
    assert.ok(block.includes('{{ item.label }} ({{ item.count }})'),
        'легенда — название и число в скобках');
});

test('каналы поиска: пустой отчёт объяснён, подпись — процент', () => {
    const block = section(CHANNELS);

    assert.ok(block.includes('{% if metrics.channels %}'),
        'пустой отчёт показывает подсказку вместо полосы');
    assert.ok(block.includes('В отчётах пока нет данных о канале.'),
        'нет пояснения для пустого отчёта');
    assert.ok(block.includes('{{ item.label }} ({{ item.count }})'),
        'легенда — название и число в скобках');
});

test('легенда стоит под полосой в каждом графике', () => {
    for (const title of CHARTS) {
        const block = section(title);
        const bar = block.indexOf('class="stack-bar"');
        // У легенды под полосой есть модификатор выравнивания, поэтому ищем без кавычки.
        const legend = block.indexOf('class="pie-legend');

        assert.notEqual(bar, -1, `график «${title}»: нет полосы`);
        assert.ok(legend > bar, `график «${title}»: легенда должна идти после полосы`);
    }
});

test('круговая диаграмма: 280 на 280 пикселей и градиент из метрик', () => {
    assertRule('.pie-chart', ['width: 280px;', 'height: 280px;', 'border-radius: 50%;']);
    assert.ok(flat(html).includes('background-image: {{ metrics.pie_css }};'),
        'доли круговой диаграммы берутся из metrics.pie_css');
});

test('CSS полосы: 48 пикселей, скругление 8, содержимое не выходит за край', () => {
    assertRule('.stack-bar', [
        'display: flex;', 'height: 48px;', 'border-radius: 8px;', 'overflow: hidden;'
    ]);
});

test('CSS сегмента: на всю высоту, подпись по центру, без переноса', () => {
    assertRule('.stack-segment', [
        'height: 100%;', 'display: flex;', 'align-items: center;', 'justify-content: center;',
        'color: white;', 'font-size: 0.85rem;', 'font-weight: 600;', 'white-space: nowrap;',
        'overflow: hidden;'
    ]);
    assertRule('.stack-segment span', ['padding: 0 8px;']);
});

test('CSS легенды: строка с квадратиком цвета 14 на 14', () => {
    assertRule('.pie-legend', ['display: flex;', 'flex-wrap: wrap;', 'gap: 16px 24px;']);
    assertRule('.legend-item', [
        'display: flex;', 'align-items: center;', 'gap: 10px;', 'font-size: 0.9rem;'
    ]);
    assertRule('.legend-color', ['width: 14px;', 'height: 14px;', 'border-radius: 4px;']);
});

test('CSS адаптивности: одна колонка на планшете, круг меньше на телефоне', () => {
    const media = flat(css);

    assert.ok(media.includes('@media (max-width: 992px) { .main-panel { grid-template-columns: 1fr;'),
        'на узком экране панель графиков должна идти в одну колонку');
    assert.ok(media.includes('@media (max-width: 768px) {'),
        'для телефона нужен отдельный набор правил');
    assert.ok(media.includes('.pie-chart { width: 220px; height: 220px; }'),
        'на телефоне круговая диаграмма уменьшается до 220 пикселей');
});

test('стили графиков лежат в общем файле, а не инлайном в шаблоне', () => {
    assert.ok(!html.includes('<style>'),
        'в metrics.html не должно быть инлайн-стилей: их не кэширует браузер');
    assert.ok(html.includes("asset_url('base.css')"),
        'страница метрик подключает общий файл стилей');
    assert.ok(html.includes("asset_url('metrics.css')"),
        'страница метрик подключает свои стили отдельным модулем');
});

test('приглашения окрашены в #11f0ec, текст — в читаемом варианте', () => {
    const matches = css.match(/--color-invite: (#[0-9a-f]{6});/gi) || [];
    const inks = css.match(/--color-invite-ink: (#[0-9a-f]{6});/gi) || [];

    assert.equal(matches.length, 2, 'цвет заливки задан для светлой и тёмной темы');
    for (const declaration of matches) {
        assert.equal(declaration.toLowerCase(), '--color-invite: #11f0ec;');
    }
    assert.equal(inks.length, 2, 'цвет текста задан для светлой и тёмной темы');
});

test('карточки конверсий красятся классами, а не инлайном', () => {
    const block = section('Воронка найма');
    const classes = html.match(/class="metric-card card-[a-z]+"/g) || [];

    assert.equal(classes.length, 4, 'у каждой карточки свой класс статуса');
    for (const name of ['card-response', 'card-offer', 'card-rejected', 'card-ignore']) {
        assert.ok(html.includes(`metric-card ${name}`), `нет класса ${name}`);
        assert.ok(css.includes(`.metric-card.${name}`), `нет стиля для .metric-card.${name}`);
    }
    assert.ok(!block.includes('border-left-color:'), 'цвет полосы задаётся в CSS, а не в шаблоне');
});
