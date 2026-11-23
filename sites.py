"""Observatory definitions and persistent last-used site preference.

Define new observatory coordinates in CUSTOM_SITES. The user's last selection
is stored separately in ``data/preferences.json``.
"""

import json
import os
from pathlib import Path
import tempfile
import threading

CUSTOM_SITES = {
    "Concordia": (123.332196, -75.09978, 3233),
    "Wannewitz": (12.457096549396859, 51.53463632237915, 100),
    "TLS": (11.7112, 50.9801, 350),
    "Tautenburg Observatory": (11.7112, 50.9801, 350),
    "Tartu": (26.4661, 58.2658, 70),
}


# User preferences are stored under data/.
# Keeping storage helpers alongside the observatory catalogue gives site-related
# code one home while allowing the application to upgrade without mutating code.
class ObservatoryPreferences:
    def __init__(self, filename):
        self.path = Path(filename)
        self._lock = threading.Lock()

    @staticmethod
    def normalize(location):
        if not isinstance(location, str):
            raise ValueError("Observatory must be a name.")
        name = location.strip()
        if not name or len(name) > 200 or any(ord(c) < 32 for c in name):
            raise ValueError("Enter an observatory name (up to 200 characters).")
        return name

    def get(self):
        """Return None when no valid preference has been saved yet."""
        with self._lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                return self.normalize(data.get("location")) if isinstance(data, dict) else None
            except (OSError, ValueError, TypeError):
                return None

    def set(self, location):
        """Write atomically so a shutdown cannot leave a half-written JSON file."""
        name = self.normalize(location)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                                 prefix=".observatory-", suffix=".tmp", delete=False) as file:
                    temporary = Path(file.name)
                    json.dump({"location": name}, file, ensure_ascii=False)
                    file.write("\n")
                os.replace(temporary, self.path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return name
