"""Current-wind input validation and caching, without external network access."""
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'wind_weather.py'
spec = importlib.util.spec_from_file_location('wind_weather', MODULE)
weather = importlib.util.module_from_spec(spec)
spec.loader.exec_module(weather)


class WindTests(unittest.TestCase):
    def setUp(self):
        weather._cache.clear()

    def test_provider_request_and_cached_speed_direction(self):
        calls = []
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        def fake_open(req, timeout):
            self.assertEqual(timeout, 5)
            self.assertIn('wind_speed_unit=ms', req.full_url)
            self.assertIn('wind_direction_10m', req.full_url)
            calls.append(req.full_url)
            return Response(json.dumps({
                'current': {'wind_speed_10m': 4.75, 'wind_direction_10m': 270,
                            'time': '2026-11-03T12:15'},
                'current_units': {'wind_speed_10m': 'm/s'},
            }).encode())
        with patch.object(weather, 'urlopen', side_effect=fake_open):
            result = weather.current_wind(-29.255, -70.74)
            self.assertEqual(result['speed_ms'], 4.75)
            self.assertEqual(result['from_degrees'], 270)
            self.assertIn('open-meteo.com/en/docs', result['source_url'])
            self.assertEqual(weather.current_wind(-29.255, -70.74), result)
        self.assertEqual(len(calls), 1, 'repeat rendering must not refetch')

    def test_reject_invalid_location_and_units(self):
        with self.assertRaises(ValueError): weather.current_wind(float('nan'), 0)
        with self.assertRaises(ValueError): weather.current_wind(91, 0)
        bad = io.BytesIO(json.dumps({'current': {'wind_speed_10m': 10, 'wind_direction_10m': 90},
                                      'current_units': {'wind_speed_10m': 'km/h'}}).encode())
        with patch.object(weather, 'urlopen', return_value=bad):
            with self.assertRaises(ValueError): weather.current_wind(0, 0)


if __name__ == '__main__': unittest.main()
