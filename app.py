import os
os.environ.setdefault("MPLBACKEND", "Agg")   # obs imports pyplot; never open a window on the server

import io
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, render_template, request, send_file, abort

from sites import CUSTOM_SITES
from store import PLOT_COLUMNS, Plan, Project, PlanStore, ProjectStore, ValidationError, parse_csv

BASE = Path(__file__).parent
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024        # 2 MB uploads
projects = ProjectStore(BASE / "data" / "projects")
plans = PlanStore(BASE / "data" / "plans")
COMPUTE_LOCK = threading.Lock()                            # one heavy astropy computation at a time

FALLBACK_LOCATIONS = ["lasilla", "paranal", "greenwich", "ohp", "keck", "lapalma"]
_locations = None

# Keep the primary CDS service first, then try the documented mirrors if a
# request times out or the service is otherwise unavailable. Each attempt has
# an HTTP timeout and a separate limit on how long the caller waits.
SIMBAD_SERVERS = (
    "https://simbad.cds.unistra.fr/simbad/",
    "https://simbad.harvard.edu/simbad/",
)
SIMBAD_TIMEOUT = 6
SIMBAD_OPTIONAL_TIMEOUT = 3


def locations():
    global _locations
    if _locations is None:
        try:
            from astropy.coordinates import EarthLocation
            sites = [s for s in EarthLocation.get_site_names() if s]
        except Exception:
            sites = FALLBACK_LOCATIONS
        custom = list(CUSTOM_SITES)
        _locations = custom + sorted(set(sites) - set(custom), key=str.lower)
    return _locations


@app.errorhandler(ValidationError)
def _bad(e):
    return jsonify(error=str(e)), 400


@app.errorhandler(KeyError)
def _missing(e):
    return jsonify(error=str(e.args[0]) if e.args else "Not found"), 404


@app.get("/")
def index():
    return render_template("index.html")


_plotly_js = None


@app.get("/vendor/plotly.min.js")
def plotly_js():
    """plotly.js as bundled with the installed python package `plotly` (no internet needed)."""
    global _plotly_js
    if _plotly_js is None:
        try:
            from plotly.offline import get_plotlyjs
            _plotly_js = get_plotlyjs()
        except ImportError:
            return "plotly (python package) is not installed", 404
    resp = Response(_plotly_js, mimetype="application/javascript")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.get("/api/locations")
def api_locations():
    return jsonify(locations())


def body():
    return request.get_json(silent=True) or {}


# ---- projects -------------------------------------------------------------
@app.get("/api/projects")
def list_projects():
    return jsonify(projects.infos())


@app.post("/api/projects")
def create_project():
    d = body()
    return jsonify(projects.create(d.get("name"), {"moon": d.get("moon"), "airmass": d.get("airmass"), "phase": d.get("phase"), "moon_velocity_limit": d.get("moon_velocity_limit"), "moon_velocity_altitude": d.get("moon_velocity_altitude", 3), "telluric_velocity_limit": d.get("telluric_velocity_limit")}).info()), 201


@app.patch("/api/projects/<name>")
def project_settings(name):
    p = projects.get(name)
    d = body()
    p.set_settings(d, apply=bool(d.get("apply")))
    return jsonify(p.info())


@app.delete("/api/projects/<name>")
def delete_project(name):
    projects.delete(name)
    return "", 204


@app.post("/api/projects/<name>/upload")
def upload(name):
    proj = projects.get(name)
    f = request.files.get("file")
    if not f:
        raise ValidationError("No file uploaded")
    try:
        text = f.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValidationError("File must be UTF-8 text")
    df, skipped = parse_csv(text, defaults=proj.defaults())
    if request.form.get("mode") == "replace":
        proj.clear()
    proj.add_many(df)
    return jsonify(added=len(df), skipped=skipped)


def csv_response(df, filename):
    buf = io.BytesIO(df.to_csv(index=False).encode())
    return send_file(buf, mimetype="text/csv", as_attachment=True, download_name=filename)


@app.get("/api/projects/<name>/download")
def download_project(name):
    p = projects.get(name)
    df = p.df[PLOT_COLUMNS[1:]]
    if "comments" in df:
        df = df[[c for c in df.columns if c != "comments"] + ["comments"]]
    return csv_response(df, f"{name}.csv")


@app.get("/api/projects/<name>/targets")
def project_targets(name):
    return jsonify(projects.get(name).records())


@app.post("/api/projects/<name>/targets")
def add_project_target(name):
    return jsonify(projects.get(name).add(body())), 201


@app.patch("/api/projects/<name>/targets/<int:tid>")
def edit_project_target(name, tid):
    return jsonify(projects.get(name).update(tid, body()))


@app.post("/api/projects/<name>/reorder")
def reorder_project(name):
    projects.get(name).reorder([int(x) for x in body().get("ids", [])])
    return jsonify(ok=True)


@app.delete("/api/projects/<name>/targets")
def delete_project_targets(name):
    projects.get(name).delete([int(i) for i in body().get("ids", [])])
    return "", 204


# ---- plans ----------------------------------------------------------------
@app.get("/api/plans")
def list_plans():
    return jsonify(plans.infos())


@app.post("/api/plans")
def create_plan():
    d = body()
    return jsonify(plans.create(d.get("name"), copy_from=d.get("copy_from"), settings=d.get("settings")).info()), 201


@app.patch("/api/plans/<name>")
def update_plan_settings(name):
    p = plans.get(name)
    p.set_settings(body())
    return jsonify(p.info())


@app.delete("/api/plans/<name>")
def delete_plan(name):
    plans.delete(name)
    return "", 204


@app.get("/api/plans/<name>/download")
def download_plan(name):
    p = plans.get(name)
    if p.settings['mode'] != 'schedule':
        return csv_response(p.df.drop(columns=['id'], errors='ignore'), f'{name}.csv')

    import datetime as dt
    import numpy as np
    import obs
    date = request.args.get('date')
    location = request.args.get('location', 'lasilla')
    if not date:
        raise ValidationError('A date is required for schedule export')
    with COMPUTE_LOCK:
        events = obs.night_data(None, date, location)['schedule_events']
    start_text = request.args.get('start_utc') or events.get(p.settings['start_of_observation'])
    if not start_text:
        raise ValidationError('The selected twilight does not occur on that night')
    try:
        start = dt.datetime.fromisoformat(start_text.replace('Z', ''))
    except ValueError:
        raise ValidationError('Invalid UTC schedule start')
    rows = []
    current = start
    for i, record in p.df.reset_index(drop=True).iterrows():
        duration = (float(record['exposure_time']) + float(record['delay'])) * int(record['exposures'])
        finish = current + dt.timedelta(seconds=duration)
        middle = current + (finish - current) / 2
        target = record.to_dict()
        # Derive start, midpoint and end observing conditions, not the preset limits.
        from astropy.coordinates import SkyCoord, AltAz, get_body
        from astropy.time import Time
        import astropy.units as u
        site = obs.get_location(location)
        sample = Time([current, middle, finish], scale='utc')
        coord = SkyCoord(str(record['ra']), str(record['dec']), unit=(u.hourangle, u.deg), frame='icrs')
        alt = coord.transform_to(AltAz(obstime=sample, location=site)).alt.deg
        with np.errstate(divide='ignore', invalid='ignore'):
            air = np.where(alt > 0, 1 / np.sin(np.deg2rad(alt)), np.nan)
        moon = get_body('moon', sample, site).separation(coord).deg
        observer_velocity, lunar_rv = obs._velocity_vectors(site, sample)
        bary, moon_velocity = obs._target_velocities(SkyCoord([coord]), observer_velocity, lunar_rv)
        rv = obs._optional_number(record, 'radial_velocity')
        def number(v):
            return round(float(v), 4) if np.isfinite(v) else None
        measured = {
            'schedule_sequence': i + 1,
            'schedule_start_utc': current.isoformat(sep=' '),
            'schedule_mid_utc': middle.isoformat(sep=' '),
            'schedule_end_utc': finish.isoformat(sep=' '),
            'scheduled_altitude_start_deg': number(alt[0]),
            'scheduled_altitude_mid_deg': number(alt[1]),
            'scheduled_altitude_end_deg': number(alt[2]),
            'scheduled_airmass_start': number(air[0]),
            'scheduled_airmass_mid': number(air[1]),
            'scheduled_airmass_end': number(air[2]),
            'scheduled_moon_distance_start_deg': number(moon[0]),
            'scheduled_moon_distance_mid_deg': number(moon[1]),
            'scheduled_moon_distance_end_deg': number(moon[2]),
            'scheduled_barycentric_velocity_start_km_s': number(bary[0, 0]),
            'scheduled_barycentric_velocity_mid_km_s': number(bary[0, 1]),
            'scheduled_barycentric_velocity_end_km_s': number(bary[0, 2]),
            'scheduled_moon_velocity_start_km_s': number(moon_velocity[0, 0]),
            'scheduled_moon_velocity_mid_km_s': number(moon_velocity[0, 1]),
            'scheduled_moon_velocity_end_km_s': number(moon_velocity[0, 2]),
            'scheduled_moon_velocity_difference_start_km_s': number(abs(moon_velocity[0, 0] - rv)) if rv is not None else None,
            'scheduled_moon_velocity_difference_mid_km_s': number(abs(moon_velocity[0, 1] - rv)) if rv is not None else None,
            'scheduled_moon_velocity_difference_end_km_s': number(abs(moon_velocity[0, 2] - rv)) if rv is not None else None,
            'scheduled_telluric_velocity_difference_start_km_s': number(abs(bary[0, 0] - rv)) if rv is not None else None,
            'scheduled_telluric_velocity_difference_mid_km_s': number(abs(bary[0, 1] - rv)) if rv is not None else None,
            'scheduled_telluric_velocity_difference_end_km_s': number(abs(bary[0, 2] - rv)) if rv is not None else None,
        }
        output = {'name': target['name'], 'ra': target['ra'], 'dec': target['dec'],
                  'gmag': target.get('gmag'), **measured,
                  'exposure_time_s': target['exposure_time'],
                  'number_of_exposures': target['exposures'],
                  'delay_s': target['delay'],
                  'default_airmass_limit': target['airmass'],
                  'default_moon_distance_limit_deg': target['moon'],
                  'default_moon_phase': target['phase'],
                  'default_moon_velocity_limit_km_s': target.get('moon_velocity_limit'),
                  'default_moon_velocity_altitude_deg': target.get('moon_velocity_altitude', 3),
                  'default_telluric_velocity_limit_km_s': target.get('telluric_velocity_limit'),
                  'default_target_radial_velocity_km_s': rv,
                  'project': target.get('project'),
                  'comments': target.get('comments')}
        rows.append(output)
        current = finish
    import pandas as pd
    return csv_response(pd.DataFrame(rows), f'{name}_schedule.csv')


@app.get("/api/plans/<name>/targets")
def plan_targets(name):
    return jsonify(plans.get(name).records())


@app.post("/api/plans/<name>/targets")
def add_plan_targets(name):
    return jsonify(added=plans.get(name).add_from(projects, body().get("items", [])))


@app.patch("/api/plans/<name>/targets/<int:tid>")
def edit_plan_target(name, tid):
    return jsonify(plans.get(name).update(tid, body()))


@app.post("/api/plans/<name>/undo")
def undo_plan(name):
    return jsonify(remaining=plans.get(name).undo())


@app.post("/api/plans/<name>/redo")
def redo_plan(name):
    return jsonify(remaining=plans.get(name).redo())


@app.post("/api/plans/<name>/reorder")
def reorder_plan(name):
    plans.get(name).reorder([int(x) for x in body().get("ids", [])])
    return jsonify(ok=True)


@app.delete("/api/plans/<name>/targets")
def delete_plan_targets(name):
    plans.get(name).delete([int(i) for i in body().get("ids", [])])
    return "", 204


# ---- bulk editing and copying between projects and plans ---------------------
@app.post("/api/targets/transfer")
def transfer_targets():
    d = body()
    def table(ref):
        if not isinstance(ref, dict) or ref.get("kind") not in ("project", "plan"):
            raise ValidationError("Invalid source or destination")
        return (projects if ref["kind"] == "project" else plans).get(str(ref.get("name", "")))
    src, dst = table(d.get("source")), table(d.get("destination"))
    ids = [int(x) for x in d.get("ids", [])]
    if not ids:
        raise ValidationError("Select at least one target")
    move = bool(d.get("move")) and isinstance(src, Plan)
    if src is dst and move:
        raise ValidationError("Cannot move targets to the same list")
    with src.lock:
        rows = src.df[src.df["id"].isin(ids)].to_dict("records")
        if len(rows) != len(set(ids)):
            raise ValidationError("One or more source targets no longer exist")
        for row in rows:
            if isinstance(dst, Plan) and isinstance(src, Project):
                row["project"], row["source_id"] = src.name, row["id"]
            elif isinstance(dst, Plan) and not isinstance(src, Plan):
                row["project"], row["source_id"] = "", 0
        count = dst.copy_rows(rows)
        if move and src is not dst:
            src.delete(ids)
    return jsonify(copied=count, moved=move)

@app.patch("/api/targets/bulk")
def bulk_update_targets():
    d = body()
    ref = d.get("target", {})
    if ref.get("kind") not in ("project", "plan"):
        raise ValidationError("Invalid target list")
    table = (projects if ref["kind"] == "project" else plans).get(str(ref.get("name", "")))
    return jsonify(updated=table.update_many(d.get("ids", []), d.get("changes", {})))


# ---- plot data (rendered in the browser) ------------------------------------
@app.get("/api/plans/<name>/plot.json")
def plot_json(name):
    plan_obj = plans.get(name)
    df = plan_obj.plot_frame()
    # Use precisely the same order as the displayed plan (including optional RA sort).
    requested = request.args.get('order', '')
    if requested:
        try:
            ids = [int(part) for part in requested.split(',')]
            if len(ids) == len(plan_obj.df) and set(ids) == set(plan_obj.df['id'].tolist()):
                positions = {int(tid): i for i, tid in enumerate(plan_obj.df['id'])}
                df = df.iloc[[positions[tid] for tid in ids]].reset_index(drop=True)
                sorted_rows = plan_obj.df.iloc[[positions[tid] for tid in ids]]
            else:
                sorted_rows = plan_obj.df
        except ValueError:
            sorted_rows = plan_obj.df
    else:
        sorted_rows = plan_obj.df
    date = request.args.get("date") or None
    location = request.args.get("location") or "lasilla"
    try:
        with COMPUTE_LOCK:
            import obs
            if request.args.get("kind") == "year":
                data = obs.year_data(df, int(date[:4]) if date else None, location, sun_altitude=float(request.args.get("sun_altitude", -12)))   # year of the chosen date
            else:
                data = obs.night_data(df, date, location)
                data["plan_settings"] = plan_obj.settings
                data["schedule_targets"] = [dict(id=int(r["id"]), name=str(r["name"]), exposure_time=float(r["exposure_time"]), exposures=int(r["exposures"]), delay=float(r["delay"])) for _,r in sorted_rows.iterrows()]
    except Exception as e:
        return jsonify(error=f"Plot failed: {e}"), 400
    return jsonify(data)




@app.get("/help")
def help_page():
    """Serve the bundled, fully rendered guide even if optional Markdown is unavailable."""
    from flask import render_template
    return render_template({"de": "help_de.html", "es": "help_es.html"}.get(request.args.get("lang", "en"), "help.html"))



@app.get("/license")
def license_page():
    from flask import Response
    return Response((BASE / "LICENSE").read_text(encoding="utf-8"), mimetype="text/plain")


def _simbad_worker(result, Simbad, server, name, query_type, timeout):
    """Run one Astroquery request in a daemon thread."""
    try:
        print(f"[SIMBAD worker] initializing for {server}", flush=True)
        # An explicit constructor timeout fetches the default server's TAP
        # capabilities before we can select a mirror. Use the offline default.
        client = Simbad()
        client.server = urlsplit(server).hostname
        # The server setter validates against Astroquery's supported mirrors.
        # Do not bypass it or mutate the global configuration on web threads.
        request = client._session.request

        def request_with_timeout(method, url, **kwargs):
            kwargs["timeout"] = timeout
            print(f"[SIMBAD HTTP] {method} {url} (timeout {timeout}s)", flush=True)
            return request(method, url, **kwargs)

        client._session.request = request_with_timeout
        print(f"[SIMBAD worker] using TAP endpoint https://{client.server}/simbad/sim-tap", flush=True)

        if query_type == "photometry":
            try:
                client.add_votable_fields("G")  # Gaia G; case-sensitive
            except Exception:
                client.add_votable_fields("flux(G)")  # older astroquery
        elif query_type == "radial_velocity":
            try:
                client.add_votable_fields("rvz_radvel")
            except Exception:
                client.add_votable_fields("rv_value")

        result["value"] = client.query_object(name)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"


def query_simbad(Simbad, name, query_type="coordinates", servers=None, timeout=SIMBAD_TIMEOUT, label="query"):
    """Query mirrors with a bounded wait (timed-out threads may still finish)."""
    last_error = None
    for server in servers or SIMBAD_SERVERS:
        started = time.monotonic()
        print(f"[SIMBAD] {label}: waiting for server {server} (timeout {timeout}s)", flush=True)
        result = {}
        worker = threading.Thread(
            target=_simbad_worker,
            args=(result, Simbad, server, name, query_type, timeout),
            daemon=True,
        )
        try:
            worker.start()
            print(f"[SIMBAD] {label}: request started via {server}", flush=True)
            worker.join(timeout)
            if worker.is_alive():
                raise TimeoutError(f"wait deadline exceeded after {timeout}s; request may still be finishing")
            if "error" in result:
                raise RuntimeError(result["error"])
            if "value" not in result:
                raise RuntimeError("SIMBAD worker returned no result")
            payload = result["value"]
            elapsed = time.monotonic() - started
            print(f"[SIMBAD] {label}: server succeeded via {server} after {elapsed:.1f}s", flush=True)
            return payload, server
        except Exception as exc:
            last_error = exc
            elapsed = time.monotonic() - started
            print(f"[SIMBAD] {label}: server failed via {server} after {elapsed:.1f}s: {exc!r}", flush=True)
            app.logger.warning("SIMBAD query failed via %s: %s", server, exc)
    raise last_error or RuntimeError("No SIMBAD server is configured")


@app.get("/api/simbad")
def simbad_lookup():
    name = request.args.get("name", "").strip()
    if not name or len(name) > 120:
        raise ValidationError("Enter a valid SIMBAD identifier (1–120 characters)")
    try:
        from astroquery.simbad import Simbad
        import astropy.units as u
        from astropy.coordinates import Angle
        # Resolve the identifier FIRST, with SIMBAD's default columns.
        # A missing Gaia G value must never turn a valid object into "not found".
        print(f"[SIMBAD] lookup requested for {name!r}", flush=True)
        table, simbad_server = query_simbad(Simbad, name, label="coordinates")
        if table is None or len(table) == 0:
            raise ValidationError("SIMBAD did not find this identifier")
        row = table[0]
        # Current astroquery returns degrees; support legacy sexagesimal output too.
        ra, dec = row["ra"], row["dec"]
        if isinstance(ra, str) or isinstance(dec, str):
            ra_angle = Angle(str(ra), unit=u.hourangle)
            dec_angle = Angle(str(dec), unit=u.deg)
        else:
            ra_angle = Angle(float(ra), unit=u.deg)
            dec_angle = Angle(float(dec), unit=u.deg)
        if not (ra_angle.degree == ra_angle.degree and dec_angle.degree == dec_angle.degree):
            raise ValidationError("SIMBAD returned invalid coordinates")
        # Optional second query for Gaia G.  Some objects (e.g. nebulae) do
        # not have a stellar Gaia magnitude, and astroquery releases differ
        # in their handling of extra fields.  Never lose valid coordinates.
        gmag = None
        try:
            import math
            import warnings
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message=".*no data corresponding.*")
                phot_table, _ = query_simbad(
                    Simbad, name, query_type="photometry",
                    servers=(simbad_server,), timeout=SIMBAD_OPTIONAL_TIMEOUT,
                    label="Gaia G")
            if phot_table is not None and len(phot_table):
                for column in phot_table.colnames:
                    if column.lower() in ("g", "flux_g", "flux(g)", "gmag", "g_mag"):
                        try:
                            value = float(phot_table[0][column])
                            if math.isfinite(value):
                                gmag = value
                                break
                        except (TypeError, ValueError):
                            pass
        except Exception:
            # Photometry is optional.  A failed additional query must not
            # stop a successful identifier/coordinate resolution.
            app.logger.debug("Optional SIMBAD Gaia-G lookup failed", exc_info=True)
        # Radial velocity is optional, just like Gaia G.  Modern astroquery
        # exposes SIMBAD's 'rvz_radvel'; older releases use 'rv_value'.
        radial_velocity = None
        try:
            rv_table, _ = query_simbad(
                Simbad, name, query_type="radial_velocity",
                servers=(simbad_server,), timeout=SIMBAD_OPTIONAL_TIMEOUT,
                label="radial velocity")
            if rv_table is not None and len(rv_table):
                import math
                for col in rv_table.colnames:
                    if col.lower() in ("rvz_radvel", "rv_value", "rvz_radvel_value", "radial_velocity"):
                        try:
                            value = float(rv_table[0][col])
                            if math.isfinite(value):
                                radial_velocity = value
                                break
                        except (ValueError, TypeError, KeyError):
                            continue
        except Exception:
            app.logger.debug("Optional SIMBAD radial velocity lookup failed", exc_info=True)
        return jsonify(radial_velocity=radial_velocity, gmag=gmag, main_id=str(row["main_id"]), ra=ra_angle.to_string(unit=u.hourangle, sep=":", precision=2, pad=True),
                       dec=dec_angle.to_string(unit=u.deg, sep=":", precision=2, pad=True, alwayssign=True))
    except ImportError:
        return jsonify(error="SIMBAD lookup requires astroquery: pip install astroquery"), 503
    except ValidationError:
        raise
    except Exception as exc:
        return jsonify(error=f"SIMBAD lookup unavailable: {exc}"), 502


@app.get("/docs/images/<filename>")
def manual_image(filename):
    if filename not in ("altitude_plot.png", "yearly_visibility_plot.png"):
        abort(404)
    return send_file(BASE / "docs" / "images" / filename)


APP_HOST = os.environ.get("SKY_PLANNER_HOST", "127.0.0.1")
APP_PORT = int(os.environ.get("SKY_PLANNER_PORT", "5511"))

# Desktop launcher session: browser page sends a heartbeat and a close signal.
# The private token prevents unrelated tabs from controlling this session.
@app.post("/api/desktop-session/<action>")
def desktop_session(action):
    import time
    import hmac
    token = os.environ.get("SKY_PLANNER_SESSION_TOKEN", "")
    folder = os.environ.get("SKY_PLANNER_SESSION_DIR", "")
    if not token or not folder or action not in ("ping", "close"):
        abort(404)
    supplied = request.get_json(silent=True) or {}
    if not hmac.compare_digest(str(supplied.get("token", "")), token):
        abort(403)
    state = Path(folder)
    # Session path is created by the trusted launcher, never from a web request.
    if action == "ping":
        (state / "heartbeat").write_text(str(time.time()))
    else:
        (state / "closed").touch()
    return "", 204


if __name__ == "__main__":
    from waitress import serve
    print(f"Sky Observation Planner available at http://{APP_HOST}:{APP_PORT}")
    serve(app, host=APP_HOST, port=APP_PORT)

