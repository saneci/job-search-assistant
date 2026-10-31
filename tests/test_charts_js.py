"""Тесты вёрстки графиков страницы метрик на Node: разметка и стили без браузера."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "tests" / "js" / "charts.test.mjs"


def test_charts_js_suite_passes() -> None:
    """Полосы, подписи сегментов и легенды графиков проверяет node --test."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js не установлен: JS-тесты вёрстки графиков пропущены")
    assert SUITE.exists(), f"нет файла тестов: {SUITE}"

    result = subprocess.run(
        [node, "--test", str(SUITE)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
