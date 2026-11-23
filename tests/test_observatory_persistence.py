"""The observatory preference must survive independent browser sessions."""
import json
from pathlib import Path
import tempfile
import time
import unittest

from sites import ObservatoryPreferences

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / 'templates' / 'index.html').read_text(encoding='utf-8')
URL = 'https://skyplanner.invalid/'


class ObservatoryPreferenceTests(unittest.TestCase):
    def test_disk_roundtrip_and_invalid_input(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'nested' / 'preferences.json'
            manager = ObservatoryPreferences(path)
            self.assertIsNone(manager.get())
            self.assertEqual(manager.set('  paranal  '), 'paranal')
            self.assertEqual(ObservatoryPreferences(path).get(), 'paranal')
            self.assertEqual(json.loads(path.read_text())['location'], 'paranal')
            for invalid in ('', '   ', 17, 'bad\nname', 'x' * 201):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    manager.set(invalid)
            self.assertEqual(manager.get(), 'paranal')
            path.write_text('{invalid', encoding='utf-8')
            self.assertIsNone(manager.get())

    def test_across_independent_browser_contexts(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('Playwright unavailable')

        with tempfile.TemporaryDirectory() as folder:
            manager = ObservatoryPreferences(Path(folder) / 'data' / 'preferences.json')
            with sync_playwright() as pw:
                try:
                    browser = pw.chromium.launch(headless=True, executable_path='/usr/bin/chromium',
                                                 args=['--no-sandbox'])
                except Exception as exc:
                    self.skipTest(f'Chromium unavailable: {exc}')
                try:
                    errors = []

                    def respond(route):
                        request = route.request
                        path = request.url.split('?', 1)[0].split('.invalid')[-1]
                        if path == '/':
                            return route.fulfill(status=200, content_type='text/html', body=INDEX_HTML)
                        if path == '/api/preferences/location':
                            if request.method == 'GET':
                                return route.fulfill(status=200, content_type='application/json', headers={'Access-Control-Allow-Origin':'*'},
                                                     body=json.dumps({'location': manager.get()}))
                            if request.method == 'POST':
                                payload = request.post_data_json
                                return route.fulfill(status=200, content_type='application/json', headers={'Access-Control-Allow-Origin':'*'},
                                                     body=json.dumps({'location': manager.set(payload['location'])}))
                        if path == '/api/projects':
                            payload = []
                        elif path == '/api/plans':
                            payload = [{'name': 'Plan 1', 'count': 0, 'mode': 'plan'}]
                        elif path == '/api/locations':
                            payload = ['lasilla', 'paranal', 'keck', 'greenwich']
                        else:
                            payload = []
                        return route.fulfill(status=200, content_type='application/json', headers={'Access-Control-Allow-Origin':'*'}, body=json.dumps(payload))

                    def new_page(old_browser_location=None):
                        context = browser.new_context()
                        context.route('**/api/**', respond)
                        page = context.new_page()
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        storage = ({'observatoryLocation': json.dumps(old_browser_location)}
                                   if old_browser_location is not None else {})
                        page.evaluate('''(values) => {
                            Object.defineProperty(window, 'localStorage', {
                                configurable: true,
                                value: {
                                    entries: values,
                                    getItem(k) { return this.entries[k] ?? null; },
                                    setItem(k, v) { this.entries[k] = String(v); }
                                }
                            });
                        }''', storage)
                        page.set_content(INDEX_HTML.replace('<head>',
                            '<head><base href="https://skyplanner.invalid/">', 1))
                        return context, page

                    def wait_for_disk(expected):
                        until = time.monotonic() + 5
                        while time.monotonic() < until:
                            if manager.get() == expected:
                                return
                            page.wait_for_timeout(50)  # service Playwright route handlers
                        self.assertEqual(manager.get(), expected)

                    # First launch: use the default and write nothing until edited.
                    context, page = new_page()
                    self.assertEqual(page.locator('#location').input_value(), 'lasilla')
                    page.locator('#location').fill('paranal')
                    page.locator('#date').click()  # committed change (blur)
                    wait_for_disk('paranal')
                    context.close()

                    # A separate browser profile has NO shared localStorage.
                    context, page = new_page()
                    page.wait_for_function("document.getElementById('location').value === 'paranal'")
                    self.assertEqual(page.evaluate("localStorage.getItem('observatoryLocation')"),
                                     '"paranal"')
                    context.close()

                    # Server preference wins over stale old browser preferences.
                    context, page = new_page('keck')
                    page.wait_for_function("document.getElementById('location').value === 'paranal'")
                    context.close()

                    # A new install with an old browser preference migrates it.
                    (Path(folder) / 'data' / 'preferences.json').unlink()
                    context, page = new_page('greenwich')
                    wait_for_disk('greenwich')
                    context.close()
                    context, page = new_page()
                    page.wait_for_function("document.getElementById('location').value === 'greenwich'")
                    context.close()
                    self.assertEqual(errors, [])
                finally:
                    browser.close()


if __name__ == '__main__':
    unittest.main()
