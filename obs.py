"""Altitude plots for observation planning.

Everything that depends only on (site, date) -- Sun events, Moon track, time grid -- is
computed once and cached, all targets are transformed in one vectorised astropy call.
"""
import calendar
import datetime
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib import dates
from matplotlib.dates import HourLocator
from astropy.time import Time
from astropy.coordinates import SkyCoord, EarthLocation, AltAz, FK5, ICRS, get_body, get_sun, get_body_barycentric_posvel
import astropy.units as u
from astropy.utils.iers import conf

from sites import CUSTOM_SITES

conf.auto_max_age = None          # do not download IERS data

FONTSIZE = 15
N_PLOT_POINTS = 60                # time samples across the night
STEPS_PER_DAY = 288
SUN_STEP = 1 / STEPS_PER_DAY      # days (5 min); grid for sun events, linear interpolation is good to ~1 s
SAMPLE_STEP = 2 / 1440            # days; time resolution of the yearly visibility (2 min)
MOON_GRID_STEP = 2 / 24           # days; Moon is computed on this grid for the yearly plot and interpolated
FAINT = 0.3                       # alpha of the track while the airmass limit is exceeded
MOON_MARGIN = 0                   # deg added to the moon-distance limit as safety buffer
LEVELS = (0.0, -12.0, -18.0)      # sun altitude: sunset/rise, nautical, astronomical twilight
HEADER = ['add', 'name', 'ra', 'dec', 'moon', 'airmass', 'phase']
PHASE_LIMITS = {'dark': .40, 'gray': .70, 'bright': 1.0}

def _phase_limits(sel):
    return np.array([PHASE_LIMITS.get(str(v).lower(), 1.0) for v in sel.get('phase', pd.Series(['bright'] * len(sel)))], dtype=float)

def _illumination(sun, moon):
    """Illuminated fraction from geocentric Sun/Moon angular separation and distances."""
    elong = sun.separation(moon).rad
    rs = sun.distance.to_value(u.km)
    rm = moon.distance.to_value(u.km)
    phase_angle = np.arctan2(rs * np.sin(elong), rm - rs * np.cos(elong))
    return np.clip((1 + np.cos(phase_angle)) / 2, 0, 1)


# --------------------------------------------------------------------------- input
def read_targets(targetlist, name='name', ra='ra', dec='dec', selector=None, moon=None, airmass=None,
                 moonlim=30, airlim=1.6, save=False):
    """Read a CSV catalogue into a plotting-ready DataFrame.

    All existing catalogue columns (including radial/velocity limits) survive.
    Targets default to selected; set ``save=True`` for an ``_opt.csv`` copy.
    """
    from pathlib import Path
    df = pd.read_csv(targetlist).rename(columns={name: 'name', ra: 'ra', dec: 'dec'})
    for col, src, default in (('add', selector, 1), ('moon', moon, moonlim),
                              ('airmass', airmass, airlim)):
        if src is not None and src != col:
            df = df.rename(columns={src: col})
        if col not in df:
            df[col] = default
    if 'phase' not in df:
        df['phase'] = 'bright'
    if save:
        path = Path(targetlist)
        df.to_csv(path.with_name(path.stem + '_opt.csv'), index=False)
    return df


def _optional_number(row, key):
    """None/empty/NaN -> None (legacy notebook dataframes lack velocity fields)."""
    v = row.get(key)
    return None if v is None or pd.isna(v) or str(v).strip() == "" else float(v)


def _velocity_vectors(location, times):
    """Velocity of observer wrt SSB and reflected sunlight wrt observer, km/s.

    Moon RV is the first-order Doppler shift for Sun -> moving lunar reflector ->
    moving observer, i.e. time derivative of the two photon path lengths.
    Does not model winds, detailed lunar surface rotation or relativistic terms.
    """
    sun_p, sun_v = get_body_barycentric_posvel('sun', times)
    moon_p, moon_v = get_body_barycentric_posvel('moon', times)
    earth_p, earth_v = get_body_barycentric_posvel('earth', times)
    site_p, site_v = location.get_gcrs_posvel(times)
    obs_p = earth_p.xyz.to_value(u.km) + site_p.xyz.to_value(u.km)
    obs_v = earth_v.xyz.to_value(u.km/u.s) + site_v.xyz.to_value(u.km/u.s)
    mp = moon_p.xyz.to_value(u.km)
    mv = moon_v.xyz.to_value(u.km/u.s)
    sp = sun_p.xyz.to_value(u.km)
    sv = sun_v.xyz.to_value(u.km/u.s)
    def projection(direction, velocity):
        return np.sum(direction * velocity, axis=0) / np.linalg.norm(direction, axis=0)
    moon_rv = projection(mp-sp, mv-sv) + projection(obs_p-mp, obs_v-mv)
    return obs_v, moon_rv


def _target_velocities(coords, obs_v, moon_rv):
    """Target barycentric correction, apparent lunar RV, and telluric RV [km/s]."""
    n = coords.icrs.cartesian.xyz.value  # (3, n_targets)
    berv = n.T @ obs_v
    return berv, moon_rv[None, :] + berv


def _velocity_masks(sel, berv, moon_apparent, moon_altitude=None):
    """Return Moon/telluric collision masks plus differences for targets without RV as NaN."""
    n, m = berv.shape
    moon_diff = np.full((n, m), np.nan)
    tel_diff = np.full((n, m), np.nan)
    blocked_moon = np.zeros((n, m), dtype=bool)
    blocked_tel = np.zeros((n, m), dtype=bool)
    for i, (_, r) in enumerate(sel.iterrows()):
        rv = _optional_number(r, 'radial_velocity')
        if rv is None:
            continue
        moon_diff[i] = np.abs(moon_apparent[i] - rv)
        tel_diff[i] = np.abs(berv[i] - rv)
        ml = _optional_number(r, 'moon_velocity_limit')
        tl = _optional_number(r, 'telluric_velocity_limit')
        if ml is not None:
            threshold = _optional_number(r, 'moon_velocity_altitude')
            threshold = 3.0 if threshold is None else threshold
            blocked_moon[i] = (moon_diff[i] < ml) & (True if moon_altitude is None else np.asarray(moon_altitude) >= threshold)
        if tl is not None:
            blocked_tel[i] = tel_diff[i] < tl
    return moon_diff, tel_diff, blocked_moon, blocked_tel


def get_location(site):
    if site in CUSTOM_SITES:
        lon, lat, height = CUSTOM_SITES[site]
        return EarthLocation.from_geodetic(lon * u.deg, lat * u.deg, height * u.m)
    return EarthLocation.of_site(site)


# --------------------------------------------------------------------------- sun geometry (numpy only)
def _crossings(jd, alt, level, falling):
    """Times (jd) at which alt crosses `level`, by linear interpolation."""
    s = alt - level
    hit = (s[:-1] >= 0) & (s[1:] < 0) if falling else (s[:-1] < 0) & (s[1:] >= 0)
    i = np.nonzero(hit)[0]
    return jd[i] + (jd[i + 1] - jd[i]) * s[i] / (s[i] - s[i + 1])


def _antitransit(jd, alt, after):
    """First local minimum of the altitude (= local solar midnight) after `after`."""
    i = np.nonzero((alt[1:-1] < alt[:-2]) & (alt[1:-1] <= alt[2:]))[0] + 1
    i = i[jd[i] > after]
    return jd[i[0]] if len(i) else after + 0.5


def _find_night(jd, alt, date_jd):
    """Night starting with the first sunset after date_jd (within 24 h).
    Without a sunset (polar day/night) the window is local midnight +-12 h.

    Returns start, end, evening events, morning events (jd; only those that exist),
    and the segment boundaries with their shading alpha (0 day .. 0.4 astronomical night)."""
    sets = _crossings(jd, alt, 0, True)
    sets = sets[(sets > date_jd) & (sets <= date_jd + 1)]
    if len(sets):
        start = sets[0]
        rises = _crossings(jd, alt, 0, False)
        rises = rises[rises > start]
        end = rises[0] if len(rises) else _antitransit(jd, alt, date_jd) + 0.5
    else:
        start = _antitransit(jd, alt, date_jd) - 0.5
        end = start + 1

    tol = 1e-9

    def inside(c):
        return c[(c >= start - tol) & (c <= end + tol)]

    def first(level, falling):
        c = inside(_crossings(jd, alt, level, falling))
        return [c[0]] if len(c) else []

    evening = [t for lv in LEVELS for t in first(lv, True)]
    morning = [t for lv in reversed(LEVELS) for t in first(lv, False)]

    inner = np.concatenate([_crossings(jd, alt, lv, f) for lv in LEVELS for f in (True, False)])
    inner = inner[(inner > start + tol) & (inner < end - tol)]
    bounds = np.sort(np.concatenate([[start], inner, [end]]))
    sun_mid = np.interp((bounds[:-1] + bounds[1:]) / 2, jd, alt)
    shade = np.select([sun_mid > 0, sun_mid > -12, sun_mid > -18], [0., 0.05, 0.2], 0.4)
    return start, end, evening, morning, bounds, shade


# --------------------------------------------------------------------------- per site/date quantities
@dataclass
class Night:
    location: EarthLocation
    times: Time                 # plotting grid
    dt: np.ndarray              # same as datetimes
    moon: SkyCoord
    moon_alt: np.ndarray        # deg
    moon_phase: np.ndarray      # illuminated fraction at each time
    evening: list               # datetimes: sunset, nautical, astronomical dusk (those that exist)
    morning: list               # datetimes: astronomical, nautical dawn, sunrise
    bounds: list                # datetimes of shading segment boundaries
    shade: np.ndarray           # alpha of each segment


def _datetimes(jd):
    return Time(np.atleast_1d(jd), format='jd').datetime


def _compute_night(location, date):
    date_jd = date.utc.jd
    jd = date_jd + np.arange(-0.5, 3.0, SUN_STEP)
    grid = Time(jd, format='jd')
    sun_alt = get_sun(grid).transform_to(AltAz(obstime=grid, location=location)).alt.deg
    start, end, evening, morning, bounds, shade = _find_night(jd, sun_alt, date_jd)

    times = Time(start - 1 / 1440 + (end - start) * np.linspace(0, 1, N_PLOT_POINTS), format='jd')
    moon = get_body('moon', times, location)
    moon_alt = moon.transform_to(AltAz(obstime=times, location=location)).alt.deg
    # Geocentric phase (same for all observatories); use the Moon at the geocentre.
    phase = _illumination(get_sun(times), get_body("moon", times))
    return Night(location, times, times.datetime, moon, moon_alt, phase,
                 list(_datetimes(evening)), list(_datetimes(morning)), list(_datetimes(bounds)), shade)


@lru_cache(maxsize=16)
def _cached_night(site, date_isot):
    return _compute_night(get_location(site), Time(date_isot))


def get_night(site, date=None):
    """Night for a site; cached unless `date` is None (= now)."""
    if date is None:
        return _compute_night(get_location(site), Time.now())
    return _cached_night(site, Time(date).isot)


# --------------------------------------------------------------------------- plotting
def _title(night):
    def fmt(d):
        return rf'{d.hour}$^{{\rm h}}${d.minute:02d}$^{{\rm m}}$'
    parts = [' | '.join(fmt(d) for d in night.evening), ' | '.join(fmt(d) for d in night.morning)]
    return 'Night times: ' + (' * '.join(p for p in parts if p) or 'no sunset/sunrise')


def _sidereal_labels(location, t):
    return t.sidereal_time('apparent', longitude=location.lon).to_string(fields=2, sep=':')


def _lst_labels(location, ticks):
    return _sidereal_labels(location, Time(dates.num2date(ticks), format='datetime'))


def _draw_base(ax, night):
    """Moon, twilight shading, axes and labels."""
    dt = night.dt
    ax.plot(dt, np.ma.masked_less(night.moon_alt, 0), label='Moon', linestyle='--', color='grey', linewidth=1.5)

    for lo, hi, alpha in zip(night.bounds[:-1], night.bounds[1:], night.shade):
        if alpha > 0:
            ax.axvspan(lo, hi, color='grey', alpha=alpha)

    ax.set_xlim(dt[0], dt[-1])
    ax.xaxis.set_major_formatter(dates.DateFormatter('%H'))
    ax.xaxis.set_major_locator(HourLocator(interval=1))
    ax.set_ylim(0, 90)
    ax.tick_params(labelsize=FONTSIZE)
    ax.set_title(_title(night), fontsize=FONTSIZE)
    ax.set_ylabel('Altitude', fontsize=FONTSIZE)
    ax.set_xlabel(f'Time from {dt[0].date()} [UTC]', fontsize=FONTSIZE)
    ax.grid(True, which='major', axis='both', linewidth=1, linestyle='--', color='grey')

    # top axis: local sidereal time
    ax_lst = ax.twiny()
    ticks = ax.get_xticks()
    labels = np.array(_lst_labels(night.location, ticks), dtype=object)
    labels[0] = labels[-1] = ''                         # first/last tick lie outside the night
    ax_lst.set_xlim(ax.get_xlim())
    ax_lst.set_xticks(ticks)
    long_night = (dt[-1] - dt[0]).total_seconds() > 0.7 * 86400
    ax_lst.set_xticklabels(labels, fontsize=FONTSIZE, rotation=40 if long_night else 0)
    ax_lst.set_xlabel('LST', fontsize=FONTSIZE)

    # right axis: airmass
    ax_x = ax_lst.twinx()
    alts = ax.get_yticks()[1:]
    ax_x.set_ylim(ax.get_ylim())
    ax_x.set_yticks(alts)
    ax_x.set_yticklabels([f'{x:.2f}' for x in 1 / np.sin(np.radians(alts))], fontsize=FONTSIZE)
    ax_x.set_ylabel('Airmass', fontsize=FONTSIZE)


def _selected(targetlist):
    return targetlist[np.array(targetlist['add']).astype(bool)].reset_index(drop=True)


def _coords(sel):
    """ICRS coordinates; RA in hourangle, Dec in degrees (any common string format)."""
    return SkyCoord(sel['ra'].astype(str).str.strip().tolist(), sel['dec'].astype(str).str.strip().tolist(),
                    frame='icrs', unit=(u.hourangle, u.deg))


def _tracks(night, sel):
    """Altitude [deg], Moon distance [deg] (both (n_targets, n_times)) and the 'too close to the Moon' mask
    for the rows of `sel`; one astropy call for all (target, time) pairs."""
    n, m = len(sel), len(night.times)
    coords = _coords(sel)
    i_star, i_time = np.repeat(np.arange(n), m), np.tile(np.arange(m), n)
    star = coords[i_star]
    alt = star.transform_to(AltAz(obstime=night.times[i_time], location=night.location)).alt.deg.reshape(n, m)
    sep = night.moon[i_time].separation(star).deg.reshape(n, m)
    near_moon = sep < (sel['moon'].to_numpy(float) + MOON_MARGIN)[:, None]
    return alt, sep, near_moon


def _draw_targets(ax, night, targetlist):
    """Plot all selected targets; returns [datetimes, airmass] of the last one (outside the Moon limit)."""
    sel = _selected(targetlist)
    n = len(sel)
    if n == 0:
        return None
    alt, sep, near_moon = _tracks(night, sel)

    limit_alt = np.degrees(np.arcsin(1 / sel['airmass'].to_numpy(float)))    # airmass limit as altitude
    for i in range(n):
        up = alt[i] >= 0
        good = up & ~near_moon[i]
        low = good & (alt[i] < limit_alt[i])                    # above the horizon, but beyond the airmass limit
        near_low = np.convolve(low, [1, 1, 1], 'same') > 0      # faint part also touches its neighbours -> no gaps
        mask = lambda keep: np.ma.masked_where(~keep, alt[i])    # masked points break the line
        line, = ax.plot(night.dt, mask(good & ~low), linestyle='-', label=sel['name'][i])
        ax.plot(night.dt, mask(good & near_low), linestyle='-', color=line.get_color(), alpha=FAINT)
        ax.plot(night.dt, mask(up & near_moon[i]), linestyle='', marker='x', markersize=10, color=line.get_color())

    with np.errstate(divide='ignore'):
        airmass = np.ma.masked_less(alt[-1], 0)
        airmass = 1 / np.sin(np.radians(airmass))
    ok = ~near_moon[-1]
    return np.array([night.dt[ok], airmass[ok]])


def plot_staralt(targetlist=None, date=None, location='lasilla', save=None, show=True):
    '''
        Altitude plot of all selected targets for the night starting on `date`.

        targetlist: dataframe with columns add, name, ra (hourangle), dec (deg), moon, airmass (see read_targets)
        date:       e.g. '2024-05-18'; None = now
        location:   astropy site name (e.g. 'ohp', 'lasilla') or one of sites.CUSTOM_SITES
        save:       file name to save the figure, None = do not save
        show:       call plt.show(); the web app passes False and takes plt.gcf()

        Returns (data, twilights): data = [datetime, airmass] of the last plotted target,
        twilights = [(datetime, alpha), ...] boundaries of the shaded twilight segments.
    '''
    night = get_night(location, date)
    fig, ax = plt.subplots(figsize=(15, 9))
    _draw_base(ax, night)
    data = _draw_targets(ax, night, targetlist) if targetlist is not None else None
    ax.legend(shadow=True, loc='center left', bbox_to_anchor=(1.1, 0.5), fontsize=FONTSIZE)
    fig.tight_layout()
    if save is not None:
        fig.savefig(save, dpi=300)
    if show:
        plt.show()
    twilights = [(night.bounds[0], 0.0)] + list(zip(night.bounds[1:], night.shade))
    return data, twilights


# --------------------------------------------------------------------------- yearly visibility
# Hours per nautical night during which a star is above its airmass limit and farther from the Moon than
# its Moon limit. Sun, stars and sidereal time are plain numpy (stars precessed to mid-year, ~0.01 deg);
# astropy is only needed for the Sun's coordinates, the (topocentric) Moon and the precession of the stars.
@dataclass
class Year:
    location: EarthLocation
    year: int
    dates: list                 # date of every night
    night_hours: np.ndarray     # length of the nautical night [h]
    night: np.ndarray           # night index of every time sample
    weight: np.ndarray          # duration of every time sample [days]
    cos_lst: np.ndarray
    sin_lst: np.ndarray
    moon_vec: np.ndarray        # (n_samples, 3) unit vector to the Moon
    moon_phase: np.ndarray      # illumination per sample
    daily_phase: np.ndarray     # illumination near midnight for every date
    lat: float                  # rad
    equinox: Time               # mid-year; precession reference for the star coordinates
    sample_jd: np.ndarray       # integration sample times for RV masks


def _unit(ra, dec):
    return np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)], axis=-1)


def _gmst(jd):
    """Greenwich mean sidereal time [rad]; UT1 ~ UTC (error < 15 arcsec)."""
    return np.radians((280.46061837 + 360.98564736629 * (jd - 2451545.0)) % 360)


def _sun_altitude(jd, sun_ra, sun_dec, lat, lon):
    """Sun altitude [deg]; all angles in rad, lon east."""
    h = _gmst(jd) + lon - sun_ra
    return np.degrees(np.arcsin(np.sin(lat) * np.sin(sun_dec) + np.cos(lat) * np.cos(sun_dec) * np.cos(h)))


def _dark_samples(jd, sun_alt, jd0, n_nights, sun_altitude=-12):
    """Time samples (cell mid points) with the Sun below the configured altitude.
    Night k is the night found by _find_night for the date jd0 + k (same definition as the altitude plot)."""
    t, w, k = [], [], []
    for night in range(n_nights):
        i0 = night * STEPS_PER_DAY
        sl = slice(i0, i0 + int(3.5 * STEPS_PER_DAY))
        bounds, shade = _find_night(jd[sl], sun_alt[sl], jd0 + night)[4:]
        if sun_altitude not in LEVELS:
            extra = np.concatenate([_crossings(jd[sl], sun_alt[sl], sun_altitude, falling)
                                    for falling in (True, False)])
            extra = extra[(extra > bounds[0]) & (extra < bounds[-1])]
            bounds = np.unique(np.concatenate([bounds, extra]))
            shade = np.zeros(len(bounds)-1)
        for a, b, s in zip(bounds[:-1], bounds[1:], shade):
            # Determine actual solar altitude at the segment midpoint. The shading
            # levels alone cannot distinguish a 0/-6 degree visibility threshold.
            mid_alt = np.interp((a+b)/2, jd, sun_alt)
            if mid_alt <= sun_altitude and b > a:
                n = max(1, int(np.ceil((b - a) / SAMPLE_STEP)))
                edges = np.linspace(a, b, n + 1)
                t.append((edges[:-1] + edges[1:]) / 2)
                w.append(np.full(n, (b - a) / n))
                k.append(np.full(n, night))
    if not t:
        return np.empty(0), np.empty(0), np.empty(0, int)
    return np.concatenate(t), np.concatenate(w), np.concatenate(k)


def _visible_hours(yr, ra_eq, dec_eq, ra_icrs, dec_icrs, moon_limit, airmass_limit, phase_limit, velocity_ok=None, velocity_blocked=None, return_violations=False):
    """(n_stars, n_nights) visible hours. ra/dec in rad; *_eq precessed to yr.equinox, *_icrs for the Moon distance."""
    out = np.zeros((len(ra_eq), len(yr.night_hours)))
    star_vec = _unit(ra_icrs, dec_icrs)
    violation_names = ('Airmass limit violated', 'Too close to the Moon', 'Moon phase limit violated',
                       'Moon vel. limit violated', 'Telluric vel. limit violated')
    violations = np.zeros((len(ra_eq), len(yr.night_hours), len(violation_names)), dtype=bool) if return_violations else None
    for i in range(len(ra_eq)):
        sin_alt = (np.sin(yr.lat) * np.sin(dec_eq[i])
                   + np.cos(yr.lat) * np.cos(dec_eq[i]) * (yr.cos_lst * np.cos(ra_eq[i]) + yr.sin_lst * np.sin(ra_eq[i])))
        high = sin_alt >= 1 / airmass_limit[i]                                   # airmass = 1 / sin(alt)
        far = yr.moon_vec @ star_vec[i] <= np.cos(np.radians(moon_limit[i] + MOON_MARGIN))
        phase_ok = yr.moon_phase <= phase_limit[i] + 1e-8
        allowed_velocity = velocity_ok[i] if velocity_ok is not None else True
        out[i] = np.bincount(yr.night, weights=yr.weight * (high & far & phase_ok & allowed_velocity), minlength=out.shape[1]) * 24
        if return_violations:
            # Report only violations while the target is above the horizon;
            # conditions affecting no possible observing time remain unmarked.
            above_horizon = sin_alt > 0
            moon_block = velocity_blocked[0][i] if velocity_blocked is not None else np.zeros_like(high)
            tel_block = velocity_blocked[1][i] if velocity_blocked is not None else np.zeros_like(high)
            conditions = (above_horizon & ~high, high & ~far, high & ~phase_ok,
                          high & moon_block, high & tel_block)
            for j, condition in enumerate(conditions):
                violations[i, :, j] = np.bincount(yr.night, weights=condition.astype(float),
                                                 minlength=out.shape[1]) > 0
    return (out, violations, violation_names) if return_violations else out


def _compute_year(location, year, sun_altitude=-12):
    n_nights = 366 if calendar.isleap(year) else 365
    jd0 = Time(f'{year}-01-01').utc.jd
    jd = jd0 - 0.5 + np.arange(n_nights * STEPS_PER_DAY + 721) * SUN_STEP
    equinox = Time(jd0 + n_nights / 2, format='jd')
    lat, lon = location.lat.rad, location.lon.rad

    # Sun: astropy on a 6 h grid, interpolated. GCRS and ICRS axes agree, so reinterpret the direction as ICRS
    # (no distance -> pure rotation) and rotate to the equator of mid-year.
    coarse = jd0 - 0.5 + np.arange(0, n_nights + 3.01, 0.25)
    sun = get_sun(Time(coarse, format='jd'))
    sun = SkyCoord(ra=sun.ra, dec=sun.dec, frame='icrs').transform_to(FK5(equinox=equinox))
    sun_ra = np.interp(jd, coarse, np.unwrap(sun.ra.rad))
    sun_dec = np.interp(jd, coarse, sun.dec.rad)
    sun_alt = _sun_altitude(jd, sun_ra, sun_dec, lat, lon)

    t, w, k = _dark_samples(jd, sun_alt, jd0, n_nights, sun_altitude)
    night_hours = np.bincount(k, weights=w, minlength=n_nights) * 24

    # Moon: astropy on a 2 h grid (topocentric), unit vectors interpolated
    mjd = jd0 - 0.5 + np.arange(0, n_nights + 3.01, MOON_GRID_STEP)
    moon = get_body('moon', Time(mjd, format='jd'), location)
    mvec = _unit(moon.ra.rad, moon.dec.rad)
    moon_vec = np.stack([np.interp(t, mjd, mvec[:, c]) for c in range(3)], axis=1)
    moon_vec /= np.linalg.norm(moon_vec, axis=1, keepdims=True)

    # Sun/Moon positions for illumination are geocentric; interpolate 2-hour samples.
    geocentric_moon = get_body("moon", Time(mjd, format="jd"))
    sun_for_phase = get_sun(Time(mjd, format="jd"))
    phase_grid = _illumination(sun_for_phase, geocentric_moon)
    moon_phase = np.interp(t, mjd, phase_grid)
    daily_phase = np.interp(jd0 + np.arange(n_nights) + .5, mjd, phase_grid)
    lst = _gmst(t) + lon
    first = datetime.date(year, 1, 1)
    return Year(location, year, [first + datetime.timedelta(days=int(d)) for d in range(n_nights)], night_hours,
                k, w, np.cos(lst), np.sin(lst), moon_vec, moon_phase, daily_phase, lat, equinox, t)


@lru_cache(maxsize=4)
def _cached_year(site, year, sun_altitude=-12):
    return _compute_year(get_location(site), year, sun_altitude)


# Ephemeris velocities are far more expensive than the visibility masks. Compute
# them once per observatory/year, using a 1-hour grid, then interpolate onto the
# existing 2-minute visibility samples. This cache also survives target edits and
# switches between different plans while the Flask process is running.
YEAR_VELOCITY_GRID_STEP = 1 / 24   # days (1 hour)


@lru_cache(maxsize=8)
def _cached_year_velocities(site, year):
    start = Time(f"{int(year)}-01-01").jd - 1
    end = Time(f"{int(year) + 1}-01-01").jd + 1
    grid = start + np.arange(int(np.ceil((end - start) / YEAR_VELOCITY_GRID_STEP)) + 1) * YEAR_VELOCITY_GRID_STEP
    obs_v, moon_rv = _velocity_vectors(get_location(site), Time(grid, format="jd"))
    return grid, obs_v, moon_rv


def _year_hours(targetlist, year, location, sun_altitude=-12, return_violations=False):
    year = int(year) if year else datetime.date.today().year
    yr = _cached_year(location, year, sun_altitude)
    sel = _selected(targetlist) if targetlist is not None else pd.DataFrame(columns=HEADER)
    hours = np.zeros((0, len(yr.dates)))
    violations = None
    violation_names = ()
    if len(sel):
        coords = _coords(sel)
        eq = coords.transform_to(FK5(equinox=yr.equinox))
        vel_ok = None
        velocity_blocked = None
        if any(_optional_number(r, 'radial_velocity') is not None and
               (_optional_number(r, 'moon_velocity_limit') is not None or
                _optional_number(r, 'telluric_velocity_limit') is not None)
               for _, r in sel.iterrows()) and len(yr.sample_jd):
            jd_grid, obs_v, moon_rv = _cached_year_velocities(location, year)
            # Interpolate the three observatory-velocity components and the
            # single lunar RV once, instead of repeating interpolation per target.
            sample_v = np.vstack([np.interp(yr.sample_jd, jd_grid, component)
                                  for component in obs_v])
            sample_moon_rv = np.interp(yr.sample_jd, jd_grid, moon_rv)
            berv, lunar = _target_velocities(coords, sample_v, sample_moon_rv)
            # Approximate lunar apparent altitude from its equatorial unit vector
            # and local sidereal direction at the integration samples.
            moon_sin_alt = (np.sin(yr.lat) * yr.moon_vec[:, 2] +
                            np.cos(yr.lat) * (yr.cos_lst * yr.moon_vec[:, 0] + yr.sin_lst * yr.moon_vec[:, 1]))
            moon_alt = np.degrees(np.arcsin(np.clip(moon_sin_alt, -1, 1)))
            _, _, blocked_moon, blocked_tel = _velocity_masks(sel, berv, lunar, moon_alt)
            vel_ok = ~(blocked_moon | blocked_tel)
            velocity_blocked = (blocked_moon, blocked_tel)
        result = _visible_hours(yr, eq.ra.rad, eq.dec.rad, coords.ra.rad, coords.dec.rad,
                               sel['moon'].to_numpy(float), sel['airmass'].to_numpy(float), _phase_limits(sel), vel_ok,
                                velocity_blocked=velocity_blocked, return_violations=return_violations)
        if return_violations:
            hours, violations, violation_names = result
        else:
            hours = result
    return (yr, year, sel, hours, violations, violation_names) if return_violations else (yr, year, sel, hours)


def plot_yearly(targetlist=None, year=None, location='lasilla', save=None, show=True):
    '''
        Hours per night each selected target is observable: above its airmass limit and farther from the
        Moon than its Moon limit (+ the same safety margin as in plot_staralt), during nautical night.

        targetlist: dataframe as for plot_staralt (selected = 'add' == 1)
        year:       None = current year
        location:   astropy site name or one of sites.CUSTOM_SITES
        Returns a DataFrame (index = date of the night, columns = nautical night length and one per target).
    '''
    yr, year, sel, hours = _year_hours(targetlist, year, location)
    names = [str(x) for x in sel['name']]

    fig, ax = plt.subplots(figsize=(15, 9))
    ax.plot(yr.dates, yr.night_hours, color='green', linestyle='--', linewidth=1, label='Nautical night')
    for name, h in zip(names, hours):
        ax.plot(yr.dates, h, linewidth=1.5, label=name)
    ax.set_xlim(yr.dates[0], yr.dates[-1])
    ax.set_ylim(0, max(1.0, 1.1 * yr.night_hours.max()))
    ax.xaxis.set_major_locator(dates.MonthLocator())
    ax.xaxis.set_major_formatter(dates.DateFormatter('%b'))
    ax.tick_params(labelsize=FONTSIZE)
    ax.set_title(f'Hours observable per nautical night, {location} {year}', fontsize=FONTSIZE)
    ax.set_xlabel('Night starting on', fontsize=FONTSIZE)
    ax.set_ylabel('Hours', fontsize=FONTSIZE)
    ax.grid(True, which='major', axis='both', linewidth=1, linestyle='--', color='grey')
    ax.legend(shadow=True, loc='center left', bbox_to_anchor=(1.01, 0.5), fontsize=FONTSIZE)
    fig.tight_layout()
    if save is not None:
        fig.savefig(save, dpi=300)
    if show:
        plt.show()
    return pd.DataFrame(np.vstack([yr.night_hours, hours]).T, index=yr.dates, columns=['nautical night'] + names)


# --------------------------------------------------------------------------- data for the web front end
def _nums(a, decimals):
    """numpy array -> list of floats with None for NaN (JSON friendly)."""
    a = np.round(np.asarray(a, float).ravel(), decimals)
    return [float(x) if np.isfinite(x) else None for x in a]


def _stamp(d):
    return d.strftime('%Y-%m-%d %H:%M:%S')           # naive UTC, read as such by the browser plot


def _title_plain(night):
    parts = [' | '.join(d.strftime('%H:%M') for d in night.evening), ' | '.join(d.strftime('%H:%M') for d in night.morning)]
    return 'Night times (UTC): ' + (' * '.join(p for p in parts if p) or 'no sunset/sunrise')


def _api_targets(targets):
    """Normalise SkyCoord, dataframe or a catalogue path to the plot schema."""
    from pathlib import Path
    if isinstance(targets, SkyCoord):
        coords = targets.icrs.reshape((1,)) if targets.isscalar else targets.icrs.flatten()
        try:
            radial = list(np.asarray(coords.radial_velocity.to_value(u.km/u.s), dtype=float))
        except (AttributeError, ValueError, TypeError):
            radial = [np.nan] * len(coords)
        return pd.DataFrame({'add': 1, 'name': [f'target_{i+1}' for i in range(len(coords))],
                             'ra': coords.ra.to_string(unit=u.hourangle, sep=':', precision=6),
                             'dec': coords.dec.to_string(unit=u.deg, sep=':', precision=6, alwayssign=True),
                             'moon': 30., 'airmass': 1.6, 'phase': 'bright',
                             'radial_velocity': radial})
    df = read_targets(targets) if isinstance(targets, (str, Path)) else targets.copy()
    if not isinstance(df, pd.DataFrame):
        raise TypeError('targets must be a SkyCoord, DataFrame or CSV path')
    for key, default in [('add', 1), ('moon', 30.), ('airmass', 1.6), ('phase', 'bright')]:
        if key not in df:
            df[key] = default
    return df


def staralt(targets, location='lasilla', date=None):
    """Return NumPy arrays for nightly target tracks (UTC, degrees, km/s).

    Structure: {'time': np.ndarray[datetime64], 'targets': {name: {
      'altitude', 'airmass', 'moon_distance', 'barycentric_velocity',
      'moon_velocity', 'moon_velocity_difference', 'telluric_velocity_difference'
      : np.ndarray}}}. Repeated target names receive a '#2' suffix.
    Missing radial velocities yield NaN difference arrays. No twilight information.
    """
    df = _selected(_api_targets(targets))
    night = get_night(location, date)
    time = np.asarray(night.dt, dtype='datetime64[ns]')
    result = {'time': time, 'targets': {}}
    if df.empty:
        return result
    altitude, moon_sep, _ = _tracks(night, df)
    with np.errstate(invalid='ignore', divide='ignore'):
        airmass = np.where(altitude > 0, 1 / np.sin(np.deg2rad(altitude)), np.nan)
    obs_v, moon_rv = _velocity_vectors(night.location, night.times)
    berv, lunar = _target_velocities(_coords(df), obs_v, moon_rv)
    moon_diff, tel_diff, _, _ = _velocity_masks(df, berv, lunar)
    for i, (_, target) in enumerate(df.iterrows()):
        name = str(target['name'])
        base = name
        suffix = 2
        while name in result['targets']:
            name = f'{base}#{suffix}'
            suffix += 1
        result['targets'][name] = {
            'altitude': np.asarray(altitude[i], dtype=float),
            'airmass': np.asarray(airmass[i], dtype=float),
            'moon_distance': np.asarray(moon_sep[i], dtype=float),
            'barycentric_velocity': np.asarray(berv[i], dtype=float),
            'moon_velocity': np.asarray(lunar[i], dtype=float),
            'moon_velocity_difference': np.asarray(moon_diff[i], dtype=float),
            'telluric_velocity_difference': np.asarray(tel_diff[i], dtype=float),
        }
    return result


def twilights(location='lasilla', date=None):
    """UTC datetimes for six solar events; absent events are None."""
    night = get_night(location, date)
    date_jd = Time(date).utc.jd if date is not None else Time.now().utc.jd
    jd = date_jd + np.arange(-0.5, 3.0, SUN_STEP)
    grid = Time(jd, format='jd')
    sun_alt = get_sun(grid).transform_to(AltAz(obstime=grid, location=night.location)).alt.deg
    lo, hi = Time(night.bounds, format='datetime').jd[[0, -1]]
    events = {}
    for name, level, falling in [('sunset', 0, True), ('nautical_dusk', -12, True),
                                 ('astronomical_dusk', -18, True),
                                 ('astronomical_dawn', -18, False),
                                 ('nautical_dawn', -12, False), ('sunrise', 0, False)]:
        hit = _crossings(jd, sun_alt, level, falling)
        hit = hit[(hit >= lo - 1e-6) & (hit <= hi + 1e-6)]
        events[name] = Time(hit[0], format='jd').datetime if len(hit) else None
    return events


def annual(targets, location='lasilla', year=None):
    """Daily ephemerides sampled at local mean solar midnight of the observatory.

    {'dates': np.ndarray[datetime64] of local calendar dates,
     'time_utc': np.ndarray[datetime64], 'targets': {name: {metric: ndarray}}}.
    'time_utc' is local mean midnight (longitude/15h offset), not time-zone midnight.
    Observability 'hours' is integrated across the associated observing night.
    """
    df = _selected(_api_targets(targets))
    year = int(year) if year is not None else datetime.date.today().year
    site = get_location(location) if not isinstance(location, EarthLocation) else location
    days = 366 if calendar.isleap(year) else 365
    dates = [datetime.date(year, 1, 1) + datetime.timedelta(days=k) for k in range(days)]
    # Local mean solar midnight is 00:00 local mean solar time: UTC = LMT - longitude/15.
    times = Time([d.isoformat() + 'T00:00:00' for d in dates], format='isot', scale='utc') - site.lon.to_value(u.deg) / 360 * u.day
    result = {'dates': np.asarray(dates, dtype='datetime64[D]'),
              'time_utc': np.asarray(times.datetime, dtype='datetime64[ns]'), 'targets': {}}
    if df.empty:
        return result
    coords = _coords(df)
    moon = get_body('moon', times, site)
    obs_v, moon_rv = _velocity_vectors(site, times)
    berv, lunar = _target_velocities(coords, obs_v, moon_rv)
    moon_diff, tel_diff, _, _ = _velocity_masks(df, berv, lunar)
    # Annual observable hours correspond to the same dates, with the configured limits.
    yr, _, sel_year, hours = _year_hours(df, year, location)
    for i, (_, target) in enumerate(df.iterrows()):
        name = str(target['name']); base = name; suffix = 2
        while name in result['targets']:
            name = f'{base}#{suffix}'; suffix += 1
        # Annual calculation may de-duplicate identical targets; look up the matching row.
        match = np.flatnonzero((sel_year['ra'].astype(str).to_numpy() == str(target['ra'])) &
                               (sel_year['dec'].astype(str).to_numpy() == str(target['dec'])) &
                               (sel_year['name'].astype(str).to_numpy() == base))
        h = hours[match[0]] if len(match) else np.full(days, np.nan)
        result['targets'][name] = {
            'observable_hours': np.asarray(h, dtype=float),
            'moon_distance': np.asarray(moon.separation(coords[i]).deg, dtype=float),
            'barycentric_velocity': np.asarray(berv[i], dtype=float),
            'moon_velocity': np.asarray(lunar[i], dtype=float),
            'moon_velocity_difference': np.asarray(moon_diff[i], dtype=float),
            'telluric_velocity_difference': np.asarray(tel_diff[i], dtype=float),
        }
    return result


def night_data(targetlist=None, date=None, location='lasilla'):
    """Everything the browser needs for the altitude plot of the selected targets (JSON-serialisable)."""
    night = get_night(location, date)
    sel = _selected(targetlist) if targetlist is not None else pd.DataFrame(columns=HEADER)
    targets = []
    if len(sel):
        alt, sep, near = _tracks(night, sel)
        shown = np.where(alt >= 0, alt, np.nan)
        with np.errstate(divide='ignore'):
            airmass = 1 / np.sin(np.radians(shown))
        obs_v, moon_rv = _velocity_vectors(night.location, night.times)
        berv, moon_apparent = _target_velocities(_coords(sel), obs_v, moon_rv)
        moon_diff, tel_diff, blocked_moon, blocked_tel = _velocity_masks(sel, berv, moon_apparent, night.moon_alt)
        for i, row in sel.iterrows():
            targets.append(dict(name=str(row['name']), moon_limit=float(row['moon']), airmass_limit=float(row['airmass']),
                                phase=str(row.get('phase', 'bright')), phase_limit=PHASE_LIMITS.get(str(row.get('phase', 'bright')), 1.0) * 100,
                                alt=_nums(shown[i], 2), airmass=_nums(airmass[i], 3), moon_sep=_nums(sep[i], 1),
                                near_moon=near[i].tolist(), moon_velocity=_nums(moon_apparent[i], 3),
                                moon_velocity_difference=_nums(moon_diff[i], 3),
                                telluric_velocity_difference=_nums(tel_diff[i], 3),
                                moon_velocity_limit=_optional_number(row, 'moon_velocity_limit'),
                                moon_velocity_altitude=3.0 if _optional_number(row, 'moon_velocity_altitude') is None else _optional_number(row, 'moon_velocity_altitude'),
                                telluric_velocity_limit=_optional_number(row, 'telluric_velocity_limit'),
                                radial_velocity=_optional_number(row, 'radial_velocity'),
                                blocked_moon_velocity=blocked_moon[i].tolist(),
                                blocked_telluric_velocity=blocked_tel[i].tolist()))
    tick = night.dt[0].replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
    ticks = []
    while tick <= night.dt[-1]:
        ticks.append(tick)
        tick += datetime.timedelta(hours=1)
    lst = [str(x) for x in _sidereal_labels(night.location, Time(ticks))] if ticks else []
    # Match the actually displayed times (which may omit crossings at polar sites)
    # to the corresponding event descriptions, in precisely the plotted order.
    evening_names = [(0., 'Sunset'), (-12., 'End of nautical twilight'),
                     (-18., 'End of astronomical twilight')]
    morning_names = [(-18., 'Beginning of astronomical twilight'),
                     (-12., 'Beginning of nautical twilight'), (0., 'Sunrise')]
    # Crossings are matched against the already computed event times. This avoids
    # claiming an event was printed when it was absent for the selected date/site.
    jd_events = Time(night.bounds, format='datetime').jd
    jd_grid = Time(night.dt, format='datetime').jd
    # Independently sample the Sun over the window, including its boundaries.
    event_jd = np.linspace(jd_events[0], jd_events[-1], max(50, int((jd_events[-1]-jd_events[0])*STEPS_PER_DAY)+1))
    event_time = Time(event_jd, format='jd')
    event_alt = get_sun(event_time).transform_to(AltAz(obstime=event_time, location=night.location)).alt.deg
    def describe_event(d, candidates, falling):
        target_jd = Time(d).jd
        hits = [(abs(h-target_jd), label) for level, label in candidates
                for h in _crossings(event_jd, event_alt, level, falling)]
        if hits:
            delta, label = min(hits)
            if delta < 5 / 1440:
                return f"{d:%H:%M} UTC — {label}"
        return f"{d:%H:%M} UTC — Solar twilight transition"
    help_lines = [describe_event(d, evening_names, True) for d in night.evening]
    help_lines += [describe_event(d, morning_names, False) for d in night.morning]
    # Explicit evening event timestamps for the scheduler. Do not infer the event
    # from array position: polar conditions can omit individual crossings.
    schedule_events = {}
    for code, altitude in [('sunset', 0.), ('civil', -6.),
                           ('nautical', -12.), ('astronomical', -18.)]:
        crossings = _crossings(event_jd, event_alt, altitude, True)
        crossings = crossings[(crossings >= jd_events[0] - 1e-8) &
                              (crossings <= jd_events[-1] + 1e-8)]
        if len(crossings):
            schedule_events[code] = _stamp(Time(crossings[0], format='jd').datetime)
    night_help = '\n'.join(help_lines) if help_lines else 'No solar horizon crossings during this night.' 
    return dict(schedule_events=schedule_events, moon_phase=_nums(night.moon_phase * 100, 1), kind='night', location=location, date=str(night.dt[0].date()), title=_title_plain(night), night_help=night_help,
                t=[_stamp(d) for d in night.dt], moon_alt=_nums(night.moon_alt, 2),
                twilight=[dict(x0=_stamp(a), x1=_stamp(b), alpha=float(al))
                          for a, b, al in zip(night.bounds[:-1], night.bounds[1:], night.shade) if al > 0],
                lst_t=[_stamp(d) for d in ticks], lst=lst, targets=targets)


def year_data(targetlist=None, year=None, location='lasilla', sun_altitude=-12):
    """Everything the browser needs for the yearly visibility plot (JSON-serialisable)."""
    if sun_altitude not in (0, -6, -12, -18):
        raise ValueError('Sun altitude must be one of 0, -6, -12, -18')
    # An identical target (including all visibility-affecting parameters) only
    # needs one expensive astronomical calculation and one Plotly curve.
    selected = _selected(targetlist) if targetlist is not None else None
    counts = []
    if selected is not None and len(selected):
        columns = ['name', 'ra', 'dec', 'moon', 'airmass', 'phase',
                   'radial_velocity', 'moon_velocity_limit', 'moon_velocity_altitude', 'telluric_velocity_limit']
        columns = [col for col in columns if col in selected.columns]
        keys = selected[columns].copy()
        # Stable, linear-time deduplication; NaN/None count as the same blank.
        from collections import Counter
        fingerprints = [tuple(None if pd.isna(v) else v for v in row)
                        for row in keys.itertuples(index=False, name=None)]
        frequencies = Counter(fingerprints)
        seen = set()
        keep_rows = []
        for i, fingerprint in enumerate(fingerprints):
            if fingerprint not in seen:
                keep_rows.append(i)
                counts.append(frequencies[fingerprint])
                seen.add(fingerprint)
        selected = selected.iloc[keep_rows].reset_index(drop=True)
    yr, year, sel, hours, violations, violation_names = _year_hours(selected, year, location, sun_altitude, return_violations=True)
    period_name = {0:'Sunset–sunrise', -6:'Civil twilight', -12:'Nautical twilight', -18:'Astronomical twilight'}[sun_altitude]
    return dict(period_label=period_name + ' visibility window', sun_altitude=sun_altitude, kind='year', location=location, year=year,
                title=f'Hours observable per {period_name.lower()} window, {location} {year}',
                dates=[d.isoformat() for d in yr.dates], night=_nums(yr.night_hours, 2), moon_phase=_nums(yr.daily_phase * 100, 1),
                targets=[dict(name=str(r['name']), duplicate_count=counts[i] if counts else 1, moon_limit=float(r['moon']), airmass_limit=float(r['airmass']),
                              phase=str(r.get('phase', 'bright')), phase_limit=PHASE_LIMITS.get(str(r.get('phase', 'bright')), 1.0) * 100,
                              hours=_nums(h, 2),
                              violations=[', '.join(violation_names[j] for j, bad in enumerate(flags) if bad)
                                          for flags in violations[i]])
                         for i, ((_, r), h) in enumerate(zip(sel.iterrows(), hours))])
