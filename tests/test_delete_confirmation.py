"""Regression: deleting targets uses a reliable in-page dialog, not window.confirm.

If playwright and Chromium are installed, this runs through the actual page with
an in-memory HTTP API. Otherwise the browser portion is skipped.
"""
import json
from pathlib import Path
import unittest

PAGE = Path(__file__).resolve().parent.parent / 'templates' / 'index.html'


def target(id_, name):
    return dict(id=id_, name=name, ra='18 36 56', dec='+38 47 01',
                ra_disp='18:36:56', dec_disp='+38:47:01', moon=30,
                airmass=1.6, phase='bright', exposure_time=1200,
                exposures=1, delay=120, comments='')


class TestDeleteConfirmation(unittest.TestCase):
    def test_native_confirm_not_used_for_destructive_actions(self):
        page = PAGE.read_text()
        for key in ('planTarget', 'projectTarget', 'project', 'plan'):
            self.assertIn("confirmationMessage('" + key + "'", page)
        self.assertIn("'selectedProject':'selectedPlan'", page)
        self.assertIn('<dialog id="confirmDlg"', page)
        self.assertIn("dialog.returnValue='cancel'", page)
        self.assertNotIn('if(!confirm(', page)

    def test_browser_confirm_cancel_and_delete(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('Playwright not installed')
        from urllib.parse import unquote, urlsplit
        state = dict(plan=[target(100, 'Vega'), target(101,'Altair'), target(102,'Deneb')],
                     project=[target(1,'Betelgeuse'), target(2,'Rigel')])
        def serve(route):
            path=unquote(urlsplit(route.request.url).path)
            method=route.request.method
            if method=='GET':
                if path=='/api/preferences/location': data={'location': None}
                elif path=='/api/locations': data=[]
                elif path=='/api/projects': data=[dict(name='Demo',count=len(state['project']),moon=30,airmass=1.6,phase='bright')]
                elif path=='/api/projects/Demo/targets': data=state['project']
                elif path=='/api/plans': data=[dict(name='Plan 1',count=len(state['plan']),mode='plan',exposure_time=1200,delay=120,start_of_observation='nautical',undo_count=0,redo_count=0)]
                elif path=='/api/plans/Plan 1/targets': data=state['plan']
                else: raise AssertionError(f'Unexpected endpoint: {path}')
            elif method=='DELETE':
                if path=='/api/plans/Plan 1/targets': key='plan'
                elif path=='/api/projects/Demo/targets': key='project'
                else: raise AssertionError(f'Unexpected delete endpoint: {path}')
                ids=json.loads(route.request.post_data)['ids']
                state[key]=[t for t in state[key] if t['id'] not in ids]
                data=dict(deleted=len(ids))
            else: raise AssertionError(f'Unexpected method {method}: {path}')
            route.fulfill(status=200,content_type='application/json',body=json.dumps(data),
                          headers={'Access-Control-Allow-Origin':'*'})

        with sync_playwright() as pw:
            try:
                browser=pw.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox'])
            except Exception as err:
                self.skipTest('Chromium not available: '+str(err))
            try:
                page=browser.new_page()
                page.route('**/api/**',serve)
                errors=[]
                page.on('pageerror',lambda e: errors.append(str(e)))
                # Local navigation is blocked in some CI environments; set the
                # real application HTML directly and intercept its API calls.
                html=PAGE.read_text().replace('<head>','<head><base href="https://skyplanner.invalid/">',1)
                page.evaluate("Object.defineProperty(window,'localStorage',{configurable:true,value:{getItem:()=>null,setItem:()=>{}}})")
                page.set_content(html)
                page.locator('#plan .card').first.wait_for()
                page.evaluate('window.confirm=()=>{throw new Error("native confirm called")}; undefined')
                remove=page.locator('#plan .card button[title="Remove this star from the plan"]').first
                remove.click()
                page.locator('#confirmDlg[open]').wait_for()
                self.assertIn('Vega',page.locator('#confirmMessage').inner_text())
                page.keyboard.press('Escape')
                page.locator('#confirmDlg:not([open])').wait_for(state='attached')
                self.assertEqual(len(state['plan']),3)
                remove.click()
                page.locator('#confirmOk').click()
                page.wait_for_function('document.querySelectorAll("#plan .card").length===2')
                self.assertEqual(len(state['plan']),2)
                # Interleaved selection rerenders and language changes.
                for language,button_text in [('de','Löschen'),('es','Eliminar'),('en','Delete'),('de','Löschen')]:
                    page.locator('#settingsBtn').click()
                    page.select_option('#uiLanguage',language)
                    page.locator('#settingsBtn').click()
                    page.locator('#plan .card .n').first.click()
                    page.locator('#planTools button').filter(has_text=button_text).first.click()
                    page.locator('#confirmDlg[open]').wait_for()
                    if language=='de': self.assertIn('aus Plan',page.locator('#confirmMessage').inner_text())
                    elif language=='es': self.assertIn('del plan',page.locator('#confirmMessage').inner_text())
                    page.locator('#confirmCancel').click()
                    page.locator('#confirmDlg:not([open])').wait_for(state='attached')
                    self.assertEqual(len(state['plan']),2)
                page.locator('#planTools button').filter(has_text='Löschen').first.click()
                page.locator('#confirmOk').click()
                page.wait_for_function('document.querySelectorAll("#plan .card").length===1')
                self.assertEqual(len(state['plan']),1)
                page.select_option('#picker','Demo')
                page.locator('#projects .card').first.wait_for()
                page.locator('#projects .card button[title="Dieses Ziel aus dem Projekt löschen"]').first.click()
                page.locator('#confirmDlg[open]').wait_for()
                page.locator('#confirmOk').click()
                page.wait_for_function('document.querySelectorAll("#projects .card").length===1')
                self.assertEqual(len(state['project']),1)
                self.assertEqual(errors,[])
            finally:
                browser.close()


if __name__=='__main__':
    unittest.main()
