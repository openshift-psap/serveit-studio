"""Lint JavaScript files with ESLint."""

import subprocess
import pytest

JS_FILES = [
    'web/static/js/modules/charts.js',
    'web/static/js/modules/config.js',
    'web/static/js/modules/navigation.js',
    'web/static/js/modules/settings.js',
    'web/static/js/report-download.js',
]


@pytest.fixture(scope='module')
def eslint_available():
    r = subprocess.run(['npx', '--yes', 'eslint@8', '--version'],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        pytest.skip('eslint not available')
    return True


@pytest.mark.parametrize('js_file', JS_FILES)
def test_js_eslint(eslint_available, js_file):
    r = subprocess.run(
        ['npx', '--yes', 'eslint@8', js_file,
         '--no-eslintrc', '--env', 'browser',
         '--parser-options=ecmaVersion:2020',
         '--rule', '{"no-undef": "off", "no-unused-vars": "off"}'],
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"ESLint errors in {js_file}:\n{r.stdout}\n{r.stderr}"
