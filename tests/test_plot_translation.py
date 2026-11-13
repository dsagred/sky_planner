"""Regression tests for GUI and Plotly strings (without a browser dependency)."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


PAGE = (Path(__file__).resolve().parent.parent / "templates" / "index.html").read_text()


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for JavaScript checks")
class TranslationTests(unittest.TestCase):
    @staticmethod
    def run_node(script: str):
        result = subprocess.run(["node", "-e", script], capture_output=True,
                                text=True, timeout=15, check=True)
        return json.loads(result.stdout)

    def test_plot_hover_strings_and_solar_title_are_localized(self):
        js = PAGE[PAGE.index("const PLOT_STRINGS = "):PAGE.index("const P = n =>")]
        result = self.run_node("""
          let uiLanguage='de';
        """ + js + """
          const de={
            title:localizedPlotTitle({kind:'night',title:'Night times (UTC): 17:45'}),
            tooltip:translateHtmlInfo('End of nautical twilight'),
            limits:translateHtmlInfo('Moon phase limit violated<br>All observing limits satisfied'),
            x:plotText('Month')
          };
          uiLanguage='es';
          const es={
            title:localizedPlotTitle({kind:'night',title:'Night times (UTC): 17:45'}),
            tooltip:translateHtmlInfo('End of nautical twilight'),
            limits:translateHtmlInfo('Moon phase limit violated<br>All observing limits satisfied'),
            x:plotText('Month')
          };
          console.log(JSON.stringify({de,es}));
        """)
        self.assertIn("Nachtzeiten", result["de"]["title"])
        self.assertEqual(result["de"]["tooltip"], "Ende der nautischen Dämmerung")
        self.assertIn("Alle Beobachtungsgrenzen", result["de"]["limits"])
        self.assertEqual(result["de"]["x"], "Monat")
        self.assertIn("Horas nocturnas", result["es"]["title"])
        self.assertEqual(result["es"]["tooltip"], "Fin del crepúsculo náutico")
        self.assertIn("Se cumplen todos", result["es"]["limits"])
        self.assertEqual(result["es"]["x"], "Mes")

    def test_dynamic_undo_redo_tooltips(self):
        js = PAGE[PAGE.index("const translateUI = text =>"):PAGE.index("if (!['altitude','fisheye']")]
        result = self.run_node("""
          let uiLanguage='de';
          const LANGUAGE_STRINGS={},EXTRA_UI_STRINGS={};
        """ + js + """
          const undo='Undo up to 30 previous changes in this plan (12 available)';
          const redo='Redo the last undone change (3 available)';
          const de=[translateUI(undo),translateUI(redo)];
          uiLanguage='es';
          const es=[translateUI(undo),translateUI(redo)];
          uiLanguage='en';
          const en=[translateUI(undo),translateUI(redo)];
          console.log(JSON.stringify({de,es,en}));
        """)
        self.assertEqual(result["de"], [
            "Bis zu 30 letzte Planänderungen rückgängig (12 verfügbar)",
            "Letzte rückgängig gemachte Änderung wiederholen (3 verfügbar)",
        ])
        self.assertIn("12 disponibles", result["es"][0])
        self.assertIn("3 disponibles", result["es"][1])
        self.assertTrue(result["en"][0].startswith("Undo up to"))

    def test_yearly_axes_and_fisheye_guides_present(self):
        self.assertIn("mirror:'allticks'", PAGE)
        self.assertIn("title: title(plotText('Month'))", PAGE)
        self.assertIn("// Faint N–S meridian", PAGE)
        self.assertIn("and E–W great-circle projection", PAGE)


if __name__ == '__main__':
    unittest.main()
