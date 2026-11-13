"""Astropy-dependent API regression checks; skipped when dependencies are absent."""
import importlib.util
import unittest


@unittest.skipUnless(importlib.util.find_spec('astropy') is not None,
                     'Astropy is not installed in this test environment')
class NightAzimuthTests(unittest.TestCase):
    def test_night_response_contains_target_and_moon_azimuth(self):
        import pandas as pd
        import obs

        targets = pd.DataFrame([dict(add=True, name='Test star', ra='12:00:00',
                                     dec='-25:00:00', moon=20, airmass=2.0,
                                     phase='bright')])
        data = obs.night_data(targets, '2026-11-02', 'Wannewitz')
        n = len(data['t'])
        self.assertEqual(data['kind'], 'night')
        self.assertEqual(len(data['moon_alt']), n)
        self.assertEqual(len(data['moon_az']), n)
        self.assertTrue(all(0 <= az < 360 for az in data['moon_az'] if az is not None))
        self.assertEqual(len(data['targets']), 1)
        target = data['targets'][0]
        self.assertEqual(len(target['az']), n)
        self.assertEqual(len(target['alt']), n)
        self.assertTrue(all(0 <= az < 360 for az in target['az'] if az is not None))


if __name__ == '__main__':
    unittest.main()
