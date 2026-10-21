"""Storage.

* Project: a named container of stars (csv) with default limits (json): data/projects/<name>.csv/.json
* Plan:    the actual observing plan, a list of star copies taken from projects: data/plans/<name>.csv
Every star keeps its coordinates exactly as entered; only the *display* strings are unified."""
import io
import math
import json
import re
import threading
from pathlib import Path

import pandas as pd

BASE_COLUMNS = ["id", "name", "ra", "dec", "gmag", "moon", "airmass", "phase", "radial_velocity", "exposure_time", "exposures", "delay", "comments", "moon_velocity_limit", "telluric_velocity_limit"]
PLAN_COLUMNS = BASE_COLUMNS + ["project", "source_id"]
PLOT_COLUMNS = ["add", "name", "ra", "dec", "moon", "airmass", "phase", "radial_velocity", "moon_velocity_limit", "telluric_velocity_limit"]   # layout expected by obs
DEFAULT_MOON = 30.0         # deg
DEFAULT_AIRMASS = 1.6
DEFAULT_PHASE = "bright"
DEFAULT_EXPOSURE_TIME = 1200
DEFAULT_EXPOSURES = 1
DEFAULT_DELAY = 120
PHASE_LIMITS = {"dark": 30, "gray": 60, "bright": 100}

def valid_phase(value):
    value = str(value or DEFAULT_PHASE).strip().lower()
    if value not in PHASE_LIMITS:
        raise ValidationError("Moon phase must be dark, gray or bright")
    return value
RA_DECIMALS = 0             # digits after the decimal point of the seconds in the displayed coordinates
DEC_DECIMALS = 0

# accepted header spellings in uploaded files -> canonical column ('add' is recognised but ignored)
ALIASES = {
    "name": {"name", "target", "targets", "object", "star"},
    "ra": {"ra", "alpha", "rightascension", "right_ascension"},
    "dec": {"dec", "delta", "declination"},
    "gmag": {"gmag", "gaia_g", "gaia_gmag", "gaia_g_magnitude"},
    "add": {"add", "selected", "select", "plot"},
    "moon": {"moon", "moonlim", "moon_limit"},
    "airmass": {"airmass", "airlim", "airmass_limit"},
    "phase": {"phase", "moon_phase", "moonphase"},
    "radial_velocity": {'rv', 'radial_velocity'},
    "exposure_time": {'exptime', 'exposure_time'},
    "exposures": {'nexp', 'number_of_exposures', 'exposures'},
    "delay": {'delay_seconds', 'delay'},
    "comments": {'notes', 'comments', 'comment'},
    "moon_velocity_limit": {'moon_rv_limit', 'moon_velocity_limit'},
    "telluric_velocity_limit": {'telluric_velocity_limit', 'telluric_rv_limit'},
}
NAME_RE = re.compile(r"^[A-Za-z0-9_\- ]{1,60}$")


class ValidationError(ValueError):
    pass


# ---------------------------------------------------------------- coordinates
_NUM = re.compile(r"\d+(?:\.\d*)?|\.\d+")


def _sexagesimal(text):
    """'19 16 18.17', '19:16:18.17', '19h16m18.17s', '-16:57:15.9', '19.27' -> (negative, value in h or deg)."""
    s = str(text).strip()
    nums = [float(x) for x in _NUM.findall(s)]
    if not nums or len(nums) > 3:
        raise ValueError(s)
    return s.startswith("-"), sum(v / 60 ** i for i, v in enumerate(nums))


def _split(value, decimals):
    """value (h or deg) -> (whole units, minutes, seconds-string) rounded to `decimals` digits of seconds."""
    q = 10 ** decimals
    total = round(value * 3600 * q)
    whole, rem = divmod(total, 3600 * q)
    minutes, sec = divmod(rem, 60 * q)
    return whole, minutes, f"{sec / q:0{2 + (decimals + 1 if decimals else 0)}.{decimals}f}"


def format_ra(text, decimals=RA_DECIMALS):
    try:
        _, v = _sexagesimal(text)
    except ValueError:
        return str(text).strip()
    q = 10 ** decimals
    v = (round(v * 3600 * q) % (24 * 3600 * q)) / (3600 * q)       # wrap 24h after rounding
    h, m, s = _split(v, decimals)
    return f"{h:02d}:{m:02d}:{s}"


def format_dec(text, decimals=DEC_DECIMALS):
    try:
        neg, v = _sexagesimal(text)
    except ValueError:
        return str(text).strip()
    d, m, s = _split(v, decimals)
    return f"{'-' if neg else '+'}{d:02d}:{m:02d}:{s}"


def validate_coords(ra, dec):
    """RA is interpreted as hourangle, Dec as degrees (same as obs.py)."""
    try:
        from astropy.coordinates import SkyCoord
        import astropy.units as u
    except ImportError:  # lenient fallback if astropy is missing
        for v in (ra, dec):
            try:
                _sexagesimal(v)
            except ValueError:
                raise ValidationError(f"Cannot parse coordinates '{ra}' '{dec}'")
        return
    try:
        SkyCoord(str(ra).strip(), str(dec).strip(), frame="icrs", unit=(u.hourangle, u.deg))
    except Exception as e:
        raise ValidationError(f"Cannot parse coordinates '{ra}' '{dec}': {e}")


# ---------------------------------------------------------------- validation / parsing
def _num(value, default, lo, hi, label):
    if value in (None, ""):
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{label} must be a number")
    if not math.isfinite(v) or not lo <= v <= hi:
        raise ValidationError(f"{label} must be between {lo} and {hi}")
    return v


def clean_target(d, require_all=True, defaults=None):
    """Validate/normalise a star dict coming from the API. `defaults`: moon/airmass of the project."""
    defaults = defaults or {"moon": DEFAULT_MOON, "airmass": DEFAULT_AIRMASS, "phase": DEFAULT_PHASE}
    out = {}
    if require_all or "name" in d:
        name = str(d.get("name", "")).strip()
        if not name:
            raise ValidationError("Name is required")
        out["name"] = name
    if require_all or "ra" in d or "dec" in d:
        ra, dec = str(d.get("ra", "")).strip(), str(d.get("dec", "")).strip()
        if not ra or not dec:
            raise ValidationError("RA and Dec are required")
        validate_coords(ra, dec)
        out["ra"], out["dec"] = ra, dec
    if require_all or "gmag" in d:
        val = d.get("gmag")
        out["gmag"] = None if val is None or str(val).strip().lower() in ("", "none", "nan") else _num(val, None, -30, 40, "Gaia G magnitude")
    if require_all or "moon" in d:
        out["moon"] = _num(d.get("moon"), defaults["moon"], 0, 180, "Moon distance")
    if require_all or "airmass" in d:
        out["airmass"] = _num(d.get("airmass"), defaults["airmass"], 1, 10, "Airmass")
    if require_all or "phase" in d:
        out["phase"] = valid_phase(d.get("phase") or defaults.get("phase", DEFAULT_PHASE))
    if require_all or "radial_velocity" in d:
        value = d.get("radial_velocity")
        out["radial_velocity"] = None if value is None or str(value).strip().lower() in ("", "none", "nan") else _num(value, None, -1e9, 1e9, "Radial velocity")
    for key, label in (("moon_velocity_limit", "Moon velocity limit"),
                       ("telluric_velocity_limit", "Telluric velocity limit")):
        if require_all or key in d:
            value = d.get(key)
            out[key] = defaults.get(key) if require_all and key not in d else (None if value is None or str(value).strip().lower() in ("", "none", "nan") else _num(value, None, 0, 1e9, label))
    for key, default, label in (("exposure_time", DEFAULT_EXPOSURE_TIME, "Exposure time"),
                                ("exposures", DEFAULT_EXPOSURES, "Number of exposures"),
                                ("delay", DEFAULT_DELAY, "Delay")):
        if require_all or key in d:
            n = _num(d.get(key), default, 0 if key != "exposures" else 1, 1e9, label)
            if key == "exposures":
                if not n.is_integer():
                    raise ValidationError(f"{label} must be a whole number")
                out[key] = int(n)
            else:
                out[key] = float(n)
    if require_all or "comments" in d:
        comments = str(d.get("comments") or "")
        if len(comments.split()) > 1000:
            raise ValidationError("Comments may contain at most 1000 words")
        out["comments"] = comments
    return out


def parse_csv(text, defaults=None):
    """Parse an uploaded csv. Accepts files with a header (any alias in ALIASES, only name/ra/dec required)
    or header-less files in the layout add,name,ra,dec,moon,airmass (or just name,ra,dec).
    Missing limits get the project defaults. Returns (DataFrame, skipped_messages)."""
    df = pd.read_csv(io.StringIO(text), dtype=str, skipinitialspace=True).dropna(how="all")
    mapping = {}
    for col in df.columns:
        key = str(col).strip().lower()
        for canon, names in ALIASES.items():
            if key in names and canon not in mapping.values():
                mapping[col] = canon
    if not {"name", "ra", "dec"} <= set(mapping.values()):
        # no usable header -> header-less file; first row is data
        df = pd.read_csv(io.StringIO(text), dtype=str, header=None, skipinitialspace=True).dropna(how="all")
        if df.shape[1] == 6:
            df.columns = ["add", "name", "ra", "dec", "moon", "airmass"]
        elif df.shape[1] == 7:
            df.columns = ["add", "name", "ra", "dec", "moon", "airmass", "phase", "radial_velocity", "exposure_time", "exposures", "delay", "comments", "moon_velocity_limit", "telluric_velocity_limit"]
        elif df.shape[1] == 3:
            df.columns = ["name", "ra", "dec"]
        else:
            raise ValidationError(
                "Could not recognise the file: need a header with name/ra/dec "
                "or a header-less file with 3 (name,ra,dec) or 6 (add,name,ra,dec,moon,airmass) columns")
    else:
        df = df.rename(columns=mapping)[list(mapping.values())]

    rows, skipped = [], []
    for i, r in df.iterrows():
        d = {k: (v.strip() if isinstance(v, str) else v) for k, v in r.items() if pd.notna(v) and k != "add"}
        try:
            rows.append(clean_target(d, defaults=defaults))
        except ValidationError as e:
            skipped.append(f"row {i + 1} ({d.get('name', '?')}): {e}")
    return pd.DataFrame(rows, columns=BASE_COLUMNS[1:]), skipped


# ---------------------------------------------------------------- tables
class Table:
    """A table of stars, persisted as csv after every change."""
    columns = BASE_COLUMNS
    dtypes = {"id": int, "name": str, "ra": str, "dec": str, "gmag": float, "moon": float, "airmass": float, "phase": str, "radial_velocity": float, "exposure_time": float, "exposures": int, "delay": float, "comments": str, "moon_velocity_limit": float, "telluric_velocity_limit": float}

    def __init__(self, name, folder):
        self.name, self.path, self.lock = name, Path(folder) / f"{name}.csv", threading.RLock()
        self.df = self._load()
        self._next = int(self.df["id"].max()) + 1 if len(self.df) else 1
        self.save()

    def _load(self):
        df = pd.read_csv(self.path, dtype=str) if self.path.exists() else pd.DataFrame()
        df = df.reindex(columns=self.columns)
        fill = {"gmag": float("nan"), "moon": DEFAULT_MOON, "airmass": DEFAULT_AIRMASS, "phase": DEFAULT_PHASE, "project": "", "source_id": 0, "radial_velocity": float("nan"), "exposure_time": DEFAULT_EXPOSURE_TIME, "exposures": DEFAULT_EXPOSURES, "delay": DEFAULT_DELAY, "comments": "", "moon_velocity_limit": float("nan"), "telluric_velocity_limit": float("nan")}
        for key in ("exposure_time", "exposures", "delay"):
            if key in df:
                df[key] = df[key].replace(r"(?i)^\s*(?:none|null|nan)?\s*$", pd.NA, regex=True)
        df = df.fillna({k: v for k, v in fill.items() if k in self.columns})
        return df.astype({k: t for k, t in self.dtypes.items() if k in self.columns})

    def defaults(self):
        return {"moon": DEFAULT_MOON, "airmass": DEFAULT_AIRMASS, "phase": DEFAULT_PHASE}

    def save(self):
        self.df.to_csv(self.path, index=False)

    def records(self):
        out = self.df.to_dict("records")
        for r in out:
            for key in ("gmag", "radial_velocity", "moon_velocity_limit", "telluric_velocity_limit"):
                r[key] = None if pd.isna(r[key]) or str(r[key]).strip().lower() in ("", "none") else float(r[key])
            r["ra_disp"], r["dec_disp"] = format_ra(r["ra"]), format_dec(r["dec"])
        return out

    def _idx(self, tid):
        hit = self.df.index[self.df["id"] == tid]
        if len(hit) == 0:
            raise KeyError(f"Unknown star {tid}")
        return hit[0]

    def add_many(self, new_df):
        with self.lock:
            new_df = new_df.copy()
            new_df.insert(0, "id", range(self._next, self._next + len(new_df)))
            self._next += len(new_df)
            self.df = pd.concat([self.df, new_df[self.columns]], ignore_index=True)
            self.save()

    def add(self, d):
        self.add_many(pd.DataFrame([clean_target(d, defaults=self.defaults())]))
        return self.records()[-1]

    def update(self, tid, d):
        with self.lock:
            i = self._idx(tid)
            for k, v in clean_target(d, require_all=False, defaults=self.defaults()).items():
                self.df.at[i, k] = v
            self.save()
            return self.records()[i]

    def update_many(self, tids, changes):
        """Validate all rows before committing a bulk change."""
        with self.lock:
            ids = [int(x) for x in tids]
            if not ids or not changes:
                raise ValidationError("Select targets and fields to modify")
            missing = set(ids) - set(self.df["id"].tolist())
            if missing:
                raise ValidationError(f"Unknown target IDs: {sorted(missing)}")
            allowed = {"name", "ra", "dec", "moon", "airmass", "phase"}
            if set(changes) - allowed:
                raise ValidationError("Unsupported fields")
            if ("ra" in changes) != ("dec" in changes):
                raise ValidationError("RA and Dec must be updated together")
            candidate = self.df.copy()
            for i in candidate.index[candidate["id"].isin(ids)]:
                merged = {k: candidate.at[i, k] for k in allowed}
                merged.update(changes)
                clean = clean_target(merged, defaults=self.defaults())
                for key in changes:
                    candidate.at[i, key] = clean[key]
            self.df = candidate
            self.save()
            return len(ids)

    def copy_rows(self, rows):
        """Add validated snapshots with fresh IDs; provenance is retained for plans."""
        with self.lock:
            incoming = []
            for r in rows:
                clean = clean_target(r, defaults=self.defaults())
                if isinstance(self, Plan):
                    clean.update(project=str(r.get("project", "")), source_id=int(r.get("source_id", 0)))
                incoming.append(clean)
            if incoming:
                self.add_many(pd.DataFrame(incoming))
            return len(incoming)

    def delete(self, tids):
        with self.lock:
            self.df = self.df[~self.df["id"].isin(tids)].reset_index(drop=True)
            self.save()

    def clear(self):
        with self.lock:
            self.df = self.df.iloc[0:0]
            self.save()

    def remove_files(self):
        self.path.unlink(missing_ok=True)


class Project(Table):
    """Container of stars with default limits for new stars."""

    def __init__(self, name, folder):
        self.meta = Path(folder) / f"{name}.json"
        self.settings = {"moon": DEFAULT_MOON, "airmass": DEFAULT_AIRMASS, "phase": DEFAULT_PHASE, "moon_velocity_limit": None, "telluric_velocity_limit": None}
        if self.meta.exists():
            self.settings.update(json.loads(self.meta.read_text()))
        super().__init__(name, folder)

    def reorder(self, ids):
        with self.lock:
            actual = self.df["id"].tolist()
            if len(ids) != len(actual) or len(set(ids)) != len(actual) or set(ids) != set(actual):
                raise ValidationError("Order must contain each project target ID exactly once")
            self.df = self.df.set_index("id").loc[ids].reset_index()
            self.save()

    def defaults(self):
        return dict(self.settings)

    def save(self):
        super().save()
        self.meta.write_text(json.dumps(self.settings))

    def remove_files(self):
        super().remove_files()
        self.meta.unlink(missing_ok=True)

    def set_settings(self, d, apply=False):
        with self.lock:
            self.settings["moon"] = _num(d.get("moon"), self.settings["moon"], 0, 180, "Moon distance")
            self.settings["airmass"] = _num(d.get("airmass"), self.settings["airmass"], 1, 10, "Airmass")
            self.settings["phase"] = valid_phase(d.get("phase") or self.settings["phase"])
            for key, label in (("moon_velocity_limit", "Moon velocity limit"), ("telluric_velocity_limit", "Telluric velocity limit")):
                if key in d:
                    value = d[key]
                    self.settings[key] = None if value is None or str(value).strip().lower() in ("", "none", "nan") else _num(value, None, 0, 1e9, label)
            if apply:
                self.df["moon"], self.df["airmass"] = self.settings["moon"], self.settings["airmass"]
                self.df["phase"] = self.settings["phase"]
                for key in ("moon_velocity_limit", "telluric_velocity_limit"):
                    self.df[key] = self.settings[key]
            self.save()

    def info(self):
        return {"name": self.name, **self.settings, "count": len(self.df)}


PLAN_DEFAULTS = {"mode": "plan", "exposure_time": 1200, "delay": 120, "start_of_observation": "nautical"}

def clean_plan_settings(data, original=None):
    result = dict(original or PLAN_DEFAULTS)
    for key in ("mode", "start_of_observation"):
        if key in data:
            allowed = ("plan", "schedule") if key == "mode" else ("sunset", "civil", "nautical", "astronomical")
            if data[key] not in allowed:
                raise ValidationError(f"Invalid {key}")
            result[key] = data[key]
    for key in ("exposure_time", "delay"):
        if key in data:
            try:
                value = float(data[key])
                if not math.isfinite(value) or value < 0:
                    raise ValueError()
            except (TypeError, ValueError):
                raise ValidationError(f"{key} must be a nonnegative number")
            result[key] = value
    return result

class Plan(Table):
    """The observing plan: copies of project stars (remembering where they came from)."""
    columns = PLAN_COLUMNS
    dtypes = {**Table.dtypes, "project": str, "source_id": int}

    def __init__(self, name, folder):
        self.meta = Path(folder) / f"{name}.json"
        self.settings = dict(PLAN_DEFAULTS)
        if self.meta.exists():
            self.settings = clean_plan_settings(json.loads(self.meta.read_text()))
        super().__init__(name, folder)
        self._undo = []  # last 30 target-list changes, scoped to this plan
        self._redo = []

    def _remember(self):
        self._redo.clear()
        self._undo.append((self.df.copy(deep=True), self._next))
        if len(self._undo) > 30:
            self._undo.pop(0)

    def undo(self):
        with self.lock:
            if not self._undo:
                raise ValidationError("Nothing to undo in this plan")
            self._redo.append((self.df.copy(deep=True), self._next))
            self.df, self._next = self._undo.pop()
            self.save()
            return len(self._undo)

    def redo(self):
        with self.lock:
            if not self._redo:
                raise ValidationError("Nothing to redo in this plan")
            self._undo.append((self.df.copy(deep=True), self._next))
            if len(self._undo)>30: self._undo.pop(0)
            self.df, self._next = self._redo.pop()
            self.save()
            return len(self._redo)

    def add_many(self, new_df):
        if len(new_df):
            with self.lock:
                self._remember()
                super().add_many(new_df)

    def delete(self, tids):
        with self.lock:
            if self.df["id"].isin(tids).any():
                self._remember()
            super().delete(tids)

    def update(self, tid, data):
        with self.lock:
            self._remember()
            return super().update(tid, data)

    def save(self):
        super().save()
        self.meta.write_text(json.dumps(self.settings))

    def set_settings(self, data):
        with self.lock:
            settings = clean_plan_settings(data, self.settings)
            if settings["mode"] == "schedule" and (data.get("apply_exposure") or data.get("apply_delay")):
                if len(self.df):
                    self._remember()
                if data.get("apply_exposure"):
                    self.df["exposure_time"] = settings["exposure_time"]
                if data.get("apply_delay"):
                    self.df["delay"] = settings["delay"]
            self.settings = settings
            self.save()

    def remove_files(self):
        super().remove_files()
        self.meta.unlink(missing_ok=True)

    def add_from(self, projects, items):
        """items: [{'project': name, 'id': star id}]. The same star may be added any number of times."""
        with self.lock:
            rows = []
            for it in items:
                proj = projects.get(str(it["project"]))
                hit = proj.df[proj.df["id"] == int(it["id"])]
                if hit.empty:
                    continue
                r = hit.iloc[0]
                rows.append(dict(name=r["name"], ra=r["ra"], dec=r["dec"], gmag=r["gmag"], moon=r["moon"], airmass=r["airmass"], phase=r["phase"], radial_velocity=r["radial_velocity"], exposure_time=self.settings["exposure_time"], exposures=r["exposures"], delay=self.settings["delay"], comments=r["comments"], moon_velocity_limit=r["moon_velocity_limit"], telluric_velocity_limit=r["telluric_velocity_limit"],
                                 project=proj.name, source_id=int(r["id"])))
            if rows:
                self.add_many(pd.DataFrame(rows))
            return len(rows)

    def reorder(self, ids):
        with self.lock:
            actual = self.df["id"].tolist()
            if len(ids) != len(actual) or len(set(ids)) != len(actual) or set(ids) != set(actual):
                raise ValidationError("Order must contain each plan target ID exactly once")
            if ids != actual:
                self._remember()
                self.df = self.df.set_index("id").loc[ids].reset_index()
                self.save()

    def plot_frame(self):
        """The plan in the layout obs expects (all stars selected)."""
        df = self.df[["name", "ra", "dec", "moon", "airmass", "phase", "radial_velocity", "moon_velocity_limit", "telluric_velocity_limit"]].copy()
        df.insert(0, "add", 1)
        return df[PLOT_COLUMNS].reset_index(drop=True)

    def info(self):
        return {"name": self.name, "count": len(self.df), **self.settings, "undo_count": len(self._undo), "redo_count": len(self._redo)}


# ---------------------------------------------------------------- stores
class Store:
    kind, label = Table, "item"

    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.items = {p.stem: self.kind(p.stem, self.folder) for p in sorted(self.folder.glob("*.csv"))}

    def names(self):
        return sorted(self.items)

    def infos(self):
        return [self.items[n].info() for n in self.names()]

    def get(self, name):
        try:
            return self.items[name]
        except KeyError:
            raise KeyError(f"Unknown {self.label} '{name}'")

    def _new_name(self, name):
        name = (name or "").strip()
        if not NAME_RE.match(name):
            raise ValidationError(f"{self.label.capitalize()} name: 1-60 chars, letters, digits, space, - and _ only")
        if name in self.items:
            raise ValidationError(f"A {self.label} with this name already exists")
        return name

    def delete(self, name):
        with self.lock:
            self.get(name).remove_files()
            del self.items[name]


class ProjectStore(Store):
    kind, label = Project, "project"

    def create(self, name, settings=None):
        with self.lock:
            name = self._new_name(name)
            p = Project(name, self.folder)
            try:
                p.set_settings(settings or {})
            except ValidationError:
                p.remove_files()
                raise
            self.items[name] = p
            return p


class PlanStore(Store):
    kind, label = Plan, "plan"

    def create(self, name, copy_from=None, settings=None):
        with self.lock:
            name = self._new_name(name)
            src = self.get(copy_from) if copy_from else None
            p = Plan(name, self.folder)
            if src is not None:
                p.df, p._next = src.df.copy(), src._next
                p.settings = dict(src.settings)
                p.save()
            if settings is not None:
                p.set_settings(settings)
            self.items[name] = p
            return p
