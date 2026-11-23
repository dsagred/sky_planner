"""The last chosen observatory is restored when Sky Planner is reopened."""
from pathlib import Path
import json
import unittest

PAGE = Path(__file__).resolve().parent.parent / 'templates' / 'index.html'


class RememberLocationTests(unittest.TestCase):
    def test_location_survives_page_restart(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('Playwright not installed')

        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(
                    headless=True, executable_path='/usr/bin/chromium',
                    args=['--no-sandbox'])
            except Exception as exc:
                self.skipTest(f'Chromium not available: {exc}')
            try:
                # about:blank cannot use persistent browser storage in this
                # sandbox. Transfer a mock storage snapshot to a *new* page,
                # which also provides the new JavaScript realm of a real reload.
                html = PAGE.read_text().replace(
                    '<head>', '<head><base href="https://skyplanner.invalid/">', 1)
                values = {}
                errors = []
                page = None

                def restart():
                    nonlocal page, values
                    if page is not None:
                        values = page.evaluate('localStorage.entries')
                        page.close()
                    page = browser.new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.route('**/api/**', lambda route: route.fulfill(
                        status=200, content_type='application/json',
                        body=json.dumps([])))
                    page.evaluate('''(values) => {
                        Object.defineProperty(window, 'localStorage', {
                          configurable: true,
                          value: {
                            entries: values,
                            getItem(k) { return this.entries[k] ?? null; },
                            setItem(k, v) { this.entries[k] = String(v); }
                          }
                        });
                    }''', values)
                    page.set_content(html)
                    return page.locator('#location').input_value()

                # First start uses La Silla.
                self.assertEqual(restart(), 'lasilla')
                page.locator('#location').fill('paranal')
                page.locator('#location').dispatch_event('change')
                self.assertEqual(page.evaluate(
                    "JSON.parse(localStorage.getItem('observatoryLocation'))"),
                    'paranal')
                self.assertEqual(restart(), 'paranal')

                # Observatory preferences are independent of plans and projects.
                page.locator('#location').fill('  greenwich  ')
                page.locator('#location').dispatch_event('change')
                self.assertEqual(restart(), 'greenwich')

                # Empty or damaged preferences fall back to La Silla.
                page.evaluate("localStorage.setItem('observatoryLocation', 'null')")
                self.assertEqual(restart(), 'lasilla')
                page.evaluate("localStorage.setItem('observatoryLocation', '17')")
                self.assertEqual(restart(), 'lasilla')
                self.assertEqual(errors, [])
                page.close()
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
