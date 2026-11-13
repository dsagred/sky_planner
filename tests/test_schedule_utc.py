"""Regression tests for the actual scheduler time-menu JavaScript."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

PAGE = (Path(__file__).resolve().parent.parent / 'templates' / 'index.html').read_text()


@unittest.skipUnless(shutil.which('node'), 'Node.js is needed for JavaScript checks')
class ScheduleUtcTests(unittest.TestCase):
    def test_current_time_menu_is_timezone_independent(self):
        helpers = PAGE[PAGE.index('let scheduleStartOverride=null;'):PAGE.index('function addScheduleBlocks(')]
        handler = PAGE[PAGE.index("$('#timeNow').onclick="):PAGE.index("$('#timeDefault').onclick=")]
        script = '''
          const RealDate=Date;
          let fixed='2026-11-13T20:06:47.123Z';
          global.Date=class extends RealDate { constructor(...args){super(...(args.length?args:[fixed]));} };
          const nodes={}; let closed=0, renders=0, messages=[];
          const $=s=>nodes[s]??=(s==='#scheduleTimeDlg'?{close(){closed++;}}:{});
          const toast=s=>messages.push(s), translateUI=s=>s;
          const lastData={kind:'night'},renderPlot=()=>{renders++;};
        ''' + helpers + handler + '''
          function click(now,start,end){
            fixed=now;lastScheduleRange={start:Date.parse(start),end:Date.parse(end)};
            const before=scheduleStartOverride,oldClosed=closed,oldRenders=renders;
            $('#timeNow').onclick();
            return {value:scheduleStartOverride===null?null:new Date(scheduleStartOverride).toISOString(),
              unchanged:before===scheduleStartOverride,closed:closed-oldClosed,renders:renders-oldRenders,
              message:messages.at(-1)||null};
          }
          const results=[
            click('2026-11-13T20:06:47.123Z','2026-11-13T16:00:00Z','2026-11-14T06:00:00Z'),
            click('2026-11-13T01:42:00Z','2026-11-13T16:00:00Z','2026-11-14T06:00:00Z'),
            click('2026-07-10T23:10:00Z','2026-12-31T16:00:00Z','2027-01-01T06:00:00Z'),
            click('2026-07-10T00:15:00Z','2026-12-31T16:00:00Z','2027-01-01T06:00:00Z'),
            click('2026-07-10T12:00:00Z','2026-12-31T16:00:00Z','2027-01-01T06:00:00Z'),
          ];
          console.log(JSON.stringify(results));
        '''
        for zone in ['UTC','Europe/Berlin','America/Los_Angeles','Asia/Kolkata','Pacific/Auckland']:
            with self.subTest(zone=zone):
                out = subprocess.run(['node','-e',script], env={**os.environ,'TZ':zone},
                                     capture_output=True,text=True,check=True,timeout=15)
                result=json.loads(out.stdout)
                self.assertEqual([r['value'] for r in result], [
                    '2026-11-13T20:06:47.123Z','2026-11-14T01:42:00.000Z',
                    '2026-12-31T23:10:00.000Z','2027-01-01T00:15:00.000Z',
                    '2027-01-01T00:15:00.000Z'])
                self.assertTrue(all(r['closed']==1 and r['renders']==1 for r in result[:4]))
                self.assertTrue(result[4]['unchanged'])
                self.assertEqual(result[4]['closed'],1)
                self.assertEqual(result[4]['renders'],0)
                self.assertIn('12:00 UTC',result[4]['message'])

if __name__=='__main__':
    unittest.main()
