"""Small dependency-free checks of the sky projection used by the browser.

Uses Node.js only if available. For full in-browser rendering, inspect the
Altitude/Fisheye controls with Plotly; the app itself does not require Node.
"""
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
class FisheyeProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (ROOT / "templates" / "index.html").read_text()
        cls.helpers = source[source.index("function skyPoint("):source.index("function twilightRibbonColor(")]
        cls.wind_helpers = (source[source.index("let windWeather=null,"):source.index("function fetchFisheyeWind(")]
            + source[source.index("function fisheyePlotRadius("):source.index("function renderFisheye(")])

    def run_js(self, assertions):
        js = """
const assert = require('node:assert/strict');
const utcMs = s => Date.parse(s.replace(' ','T') + 'Z');
""" + self.helpers + "\n" + assertions
        process = subprocess.run(["node", "-e", js], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)

    def test_zenith_horizon_compass_and_below_horizon(self):
        self.run_js("""
const close=(a,b)=>assert.ok(Math.abs(a-b)<1e-10,`${a} != ${b}`);
const zenith=skyPoint(90,243);close(zenith.x,0);close(zenith.y,0);
const north=skyPoint(0,0);close(north.x,0);close(north.y,1);
const east=skyPoint(0,90);close(east.x,-1);close(east.y,0);
const south=skyPoint(0,180);close(south.y,-1);
const west=skyPoint(0,270);close(west.x,1);
close(Math.hypot(skyPoint(30,320).x,skyPoint(30,320).y),2/3);
assert.equal(skyPoint(-1,120),null);
assert.equal(skyPoint(null,120),null);
""")

    def test_fractional_horizon_limit_reprojects_and_clips(self):
        self.run_js("""
const close=(a,b)=>assert.ok(Math.abs(a-b)<1e-10,`${a} != ${b}`);
close(Math.hypot(skyPoint(20.5,90,20.5).x,skyPoint(20.5,90,20.5).y),1);
close(Math.hypot(skyPoint(55.25,0,20.5).x,skyPoint(55.25,0,20.5).y),.5);
assert.equal(skyPoint(20.49,180,20.5),null);
close(Math.hypot(skyPoint(90,90,20.5).x,skyPoint(90,90,20.5).y),0);
const times=['2026-11-02 18:00:00','2026-11-02 18:20:00','2026-11-02 18:40:00'];
const samples=skySamples([15,25,45],[0,90,180],times,20.5);
assert.equal(samples[0].x,null);assert.ok(samples[1].x<0);
""")

    def test_wind_overlay_only_expands_axes_near_edges(self):
        self.run_js("""
let showFisheyeWind=true;
""" + self.wind_helpers + """
const close=(a,b)=>assert.ok(Math.abs(a-b)<1e-10,`${a} != ${b}`);
windWeather={from_degrees:315};windWeatherSite='lasilla';
close(fisheyePlotRadius('lasilla'),1.11);
windWeather.from_degrees=45;close(fisheyePlotRadius('lasilla'),1.11);
windWeather.from_degrees=0;assert.ok(fisheyePlotRadius('lasilla')>1.11);
windWeather.from_degrees=90;assert.ok(fisheyePlotRadius('lasilla')>1.11);
windWeather.from_degrees=270;assert.ok(fisheyePlotRadius('lasilla')>1.11);
close(fisheyePlotRadius('other-observatory'),1.11);
showFisheyeWind=false;close(fisheyePlotRadius('lasilla'),1.11);
""")

    def test_window_interpolation_wrap_and_coloured_area(self):
        self.run_js("""
const times=['2026-11-02 18:00:00','2026-11-02 18:20:00','2026-11-02 18:40:00'];
const samples=skySamples([45,50,60],[359,1,10],times);
assert.ok(Math.abs(samples[0].x-samples[1].x)<0.05, 'azimuth wraps smoothly');
const run=skyWindow(samples,utcMs('2026-11-02 18:05:00'),utcMs('2026-11-02 18:35:00'));
assert.equal(run.length,1); assert.equal(run[0].length,3);
assert.equal(run[0][0].ms,utcMs('2026-11-02 18:05:00'));
const band=skyRibbon(run[0],0.04,'red',0.25);
assert.equal(band.fill,'toself');assert.equal(band.opacity,0.25);
assert.equal(band.x.length,7);assert.equal(band.y.length,7);
assert.equal(skyWindow(samples,utcMs('2026-11-03 18:00:00'),utcMs('2026-11-03 19:00:00')).length,0);
""")


if __name__ == "__main__":
    unittest.main()
