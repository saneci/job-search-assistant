/* Общая тема для всех страниц: выбор сохраняется между вкладками. */
(function () {
    const THEME_KEY = 'vacancy-report-theme';
    const toggle = document.getElementById('themeToggle');

    function applyTheme(theme) {
        document.documentElement.setAttribute('data-theme', theme);
        if (!toggle) return;
        toggle.textContent = theme === 'dark' ? '☀️' : '🌙';
        toggle.setAttribute(
            'aria-label',
            theme === 'dark' ? 'Включить светлую тему' : 'Включить тёмную тему'
        );
        toggle.setAttribute('title', theme === 'dark' ? 'Светлая тема' : 'Тёмная тема');
    }

    function preferredTheme() {
        const saved = localStorage.getItem(THEME_KEY);
        if (saved === 'dark' || saved === 'light') return saved;
        return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }

    applyTheme(preferredTheme());

    if (toggle) {
        toggle.addEventListener('click', () => {
            const current = document.documentElement.getAttribute('data-theme');
            const next = current === 'dark' ? 'light' : 'dark';
            applyTheme(next);
            localStorage.setItem(THEME_KEY, next);
        });
    }
})();
