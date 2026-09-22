"""Replication of SEC. 213 of the 21st Century ROAD to Housing Act (Build Now).

Downloads every public source, applies the four eligibility screens in
subsection (a)(3), computes the housing growth improvement rate, and runs the
bonus-and-penalty reallocation in subsection (b).

    python build_now.py --census-key YOUR_KEY

Inputs (shipped, in inputs/):
    jurisdictions.csv           the CDBG entitlement universe and FY2025 awards
    jurisdiction_counties.csv   jurisdiction -> county, MANY-TO-MANY: a city can
                                span several counties, which is what makes the
                                disaster screen contentious
    safmr_by_jurisdiction.csv   median Small Area Fair Market Rent per recipient.
                                Shipped rather than downloaded because HUD blocks
                                scripted pulls of the SAFMR workbook.

Downloads (cached under cache/, delete it to refresh):
    ACS 5-year   housing units, median home value, rental vacancy   [needs a key]
    BPS          Building Permits Survey, an alternative unit series
    OpenFEMA     DR and EM declarations, by designated county

Output: outputs/results.csv, one row per recipient, plus a summary to stdout.

WHY NOT THE ADDRESS COUNT LISTING FILES, WHICH THE STATUTE NAMES?
Because for these years they cannot produce the statute's own metric. Census
publishes ACL vintages for 2020, 2023, 2024, 2025 and 2026 only -- nothing
before 2020, nothing for 2021 or 2022. The prior-growth window for an FY2025
allocation runs 2014-2019 and for FY2029 runs FY2018-FY2023; the ACL reaches
neither, and not until FY2031 does a prior window (FY2020-FY2025) fall entirely
inside the published range. Any "ACL-based" growth rate for today is therefore a
splice onto another series, and in our own earlier work the splice, not the ACL,
drove the result. So this script offers the two series that can actually be
built end-to-end, and reports how much the answer moves between them:

    --series acs   ACS 5-year B25001 housing units          (default)
    --series bps   base-year ACS stock + cumulated permits

Both are hybrids of a kind -- BPS counts authorisations, not completions, and
nets no demolitions. That is the point: the statute names a source that cannot
do the job, and every substitute gives a different answer.
"""
import argparse
import io
import json
import os
import sys
import urllib.parse
import urllib.request
import zipfile

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
INPUTS = os.path.join(HERE, "inputs")
CACHE = os.path.join(HERE, "cache")
OUTPUTS = os.path.join(HERE, "outputs")

# ---------------------------------------------------------------------------
# Parameters. Every one is a reading of the statute; none is obvious.
# ---------------------------------------------------------------------------
ANCHOR_FY = 2025            # the fiscal year being allocated
ALLOCATION_DATE = "2025-05-01"   # (a)(3)(C) measures 3 years back from this

# (a)(2) current growth: Q3 of the 6th preceding FY -> Q3 of the preceding FY.
# (a)(6) prior growth:   Q3 of the 11th preceding FY -> Q3 of the 6th preceding.
CURRENT_WINDOW = (ANCHOR_FY - 6, ANCHOR_FY - 1)
PRIOR_WINDOW = (ANCHOR_FY - 11, ANCHOR_FY - 6)
UNIT_YEARS = list(range(ANCHOR_FY - 11, ANCHOR_FY))

DISASTER_LOOKBACK_YEARS = 3  # (a)(3)(C)
SAFMR_PERCENTILE = 60        # (a)(3)(A)(i)
EXTREME_GROWTH_RATE = 0.04   # (a)(4) "extremely high-growth recipient"
PENALTY_RATE = 0.10          # (b)(2)(B)

# (a)(3)(B) national annual rental vacancy rate. The Census Bureau publishes
# more than one figure for this concept; 6.8% is the CPS/HVS ANNUAL rate for
# 2024 (Annual Statistics, Table 1). The ACS 5-year 2024 figure computed on the
# same basis as the local rates is 5.5507%, and the choice moves ~170
# jurisdictions. Override with --vacancy-rate to test the alternative.
NATIONAL_RENTAL_VACANCY_RATE = 0.068

# (a)(3)(D) "lacks the legal authority to enact or update zoning and permitting
# ordinances". No reading of this clause reproduces any published coding of it,
# so the baseline applies no zoning screen at all and --zoning-rule counties
# reports the sensitivity (it excludes every county-type recipient).
ZONING_RULE = "none"

ACS_HU = "B25001_001E"        # total housing units
ACS_HOMEVAL = "B25077_001E"   # median value, owner-occupied
ACS_RENTER = "B25003_003E"    # renter-occupied
ACS_VACRENT = "B25004_002E"   # vacant, for rent


# ---------------------------------------------------------------------------
# Fetching. Everything is cached to disk so a re-run is offline and identical.
# ---------------------------------------------------------------------------
def fetch(url, tag, binary=False):
    """GET with an on-disk cache. `tag` is the cache filename."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, tag)
    if os.path.exists(path):
        mode = "rb" if binary else "r"
        with open(path, mode, **({} if binary else {"encoding": "utf-8"})) as fh:
            return fh.read()
    req = urllib.request.Request(url, headers={"User-Agent": "build-now-replication"})
    raw = urllib.request.urlopen(req, timeout=180).read()
    data = raw if binary else raw.decode("utf-8", "replace")
    mode = "wb" if binary else "w"
    with open(path, mode, **({} if binary else {"encoding": "utf-8"})) as fh:
        fh.write(data)
    return data


def acs(year, variables, geo, key):
    """One ACS 5-year call. `geo` is e.g. 'place:*&in=state:06'."""
    url = ("https://api.census.gov/data/%d/acs/acs5?get=%s&for=%s&key=%s"
           % (year, ",".join(variables), geo, key))
    tag = "acs_%d_%s_%s.json" % (year, "_".join(variables),
                                 geo.replace(":", "").replace("*", "all")
                                 .replace("&in=", "_"))
    rows = json.loads(fetch(url, tag))
    head, body = rows[0], rows[1:]
    out = []
    for r in body:
        d = dict(zip(head, r))
        # the trailing geography columns concatenate into the GEOID
        geoid = "".join(d[c] for c in head[len(variables):])
        out.append({"geoid": geoid,
                    **{v: pd.to_numeric(d[v], errors="coerce") for v in variables}})
    return pd.DataFrame(out)


def acs_calls(states, cousub_states, variables):
    """The geography scopes to pull. Counties come back nationally in one call;
    places and county subdivisions have to be requested state by state, and
    subdivisions only for the handful of states that have such recipients."""
    yield "county:*&in=state:*"
    for st in states:
        yield "place:*&in=state:%s" % st
    for st in cousub_states:
        yield "county%%20subdivision:*&in=state:%s%%20county:*" % st


def acs_panel(states, cousub_states, key):
    """Housing units per geography per year, for the two growth windows."""
    frames = []
    for year in UNIT_YEARS:
        for scope in acs_calls(states, cousub_states, [ACS_HU]):
            try:
                df = acs(year, [ACS_HU], scope, key)
            except Exception as exc:
                print("  [warn] ACS %d %s: %s" % (year, scope[:22], type(exc).__name__))
                continue
            df["year"] = year
            frames.append(df[["geoid", "year", ACS_HU]])
        print("  ACS %d done" % year, flush=True)
    panel = pd.concat(frames, ignore_index=True).rename(columns={ACS_HU: "units"})
    return panel.dropna(subset=["units"]).drop_duplicates(["geoid", "year"])


def acs_eligibility(states, cousub_states, key):
    """Median home value and rental vacancy rate, latest ACS 5-year."""
    year = ANCHOR_FY - 1
    variables = [ACS_HOMEVAL, ACS_RENTER, ACS_VACRENT]
    frames = []
    for scope in acs_calls(states, cousub_states, variables):
        try:
            frames.append(acs(year, variables, scope, key))
        except Exception:
            continue
    d = pd.concat(frames, ignore_index=True).drop_duplicates("geoid")
    d["home_value"] = d[ACS_HOMEVAL].where(d[ACS_HOMEVAL] > 0)
    denom = d[ACS_RENTER] + d[ACS_VACRENT]
    d["rental_vacancy_rate"] = (d[ACS_VACRENT] / denom).where(denom > 0)
    # national median home value, the (a)(3)(A)(ii) comparator
    us = acs(year, [ACS_HOMEVAL], "us:1", key)
    return d[["geoid", "home_value", "rental_vacancy_rate"]], float(us[ACS_HOMEVAL].iloc[0])


def fema_declarations(start, end):
    """Every DR/EM designated-area row in a date range.

    FEMA declares for a STATE and then DESIGNATES areas, one row per designated
    area, which is why a city spanning several counties can be reached by a
    declaration that named only one of them. Fire Management (FM) declarations
    are a different Stafford authority and do not qualify.
    """
    flt = ("declarationDate ge '%sT00:00:00.000z' and "
           "declarationDate le '%sT23:59:59.999z' and "
           "(declarationType eq 'DR' or declarationType eq 'EM')"
           % (start.date().isoformat(), end.date().isoformat()))
    fields = ("fipsStateCode,fipsCountyCode,declarationType,declarationDate,"
              "disasterNumber,incidentType")
    rows, skip = [], 0
    while True:
        qs = urllib.parse.urlencode({"$filter": flt, "$select": fields,
                                     "$top": 1000, "$skip": skip,
                                     "$orderby": "declarationDate"})
        url = "https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries?" + qs
        # the cache tag carries BOTH endpoints: a run without --extras stops at
        # the allocation date, and reusing that cache for a later --extras run
        # would silently drop the most recent year of declarations
        got = json.loads(fetch(url, "fema_%s_%s_%d.json"
                               % (start.date(), end.date(), skip)))
        batch = got.get("DisasterDeclarationsSummaries", [])
        if not batch:
            break
        rows += batch
        skip += 1000
    df = pd.DataFrame(rows)
    df = df[df.fipsCountyCode.astype(str) != "000"].copy()
    df["cg"] = (df.fipsStateCode.astype(str).str.zfill(2)
                + df.fipsCountyCode.astype(str).str.zfill(3))
    df["date"] = pd.to_datetime(df.declarationDate.str[:10])
    print("  FEMA: %d designated-area rows, %d distinct counties, %s..%s"
          % (len(df), df.cg.nunique(), start.date(), end.date()))
    return df[["disasterNumber", "cg", "date", "incidentType"]]


def designated_in(decl, end, years=DISASTER_LOOKBACK_YEARS):
    """Counties designated in the `years` before `end`."""
    start = end - pd.DateOffset(years=years)
    return set(decl[(decl.date > start) & (decl.date <= end)].cg)


BPS_BASE = "https://www2.census.gov/econ/bps/"
BPS_REGIONS = [("Northeast", "ne"), ("Midwest", "mw"), ("South", "so"), ("West", "we")]


def _bps_rows(text):
    """Data rows only: the files carry two header lines and a blank separator."""
    import csv as _csv
    for row in _csv.reader(io.StringIO(text)):
        if row and row[0].strip().isdigit() and len(row[0].strip()) == 4:
            yield row


def bps_permits(want_place, want_county, want_cousub):
    """Building Permits Survey: units authorised per jurisdiction per year.

    Collected for comparison only. The statute names Census address files, and
    permits are authorisations rather than completions and net no demolitions,
    so this is not a drop-in substitute for the ACS or ACL series.
    """
    out, seen = [], set()
    for year in UNIT_YEARS:
        yy = str(year)
        try:  # counties: one national file a year
            for p in _bps_rows(fetch(BPS_BASE + "County/co%sa.txt" % yy,
                                     "bps_co%s.txt" % yy)):
                g = p[1] + p[2]
                if g in want_county:
                    out.append({"geoid": g, "year": year,
                                "bps_units": sum(int(p[i]) for i in (7, 10, 13, 16)
                                                 if p[i].strip())})
        except Exception as exc:
            print("  [warn] BPS county %d: %s" % (year, type(exc).__name__))
        for region, pre in BPS_REGIONS:  # places and MCDs, by region
            try:
                txt = fetch(BPS_BASE + "Place/%s%%20Region/%s%sa.txt" % (region, pre, yy),
                            "bps_%s%s.txt" % (pre, yy))
            except Exception:
                continue
            for p in _bps_rows(txt):
                units = sum(int(p[i]) for i in (18, 21, 24, 27) if p[i].strip())
                state, place, mcd, county = p[1], p[5].strip(), p[6].strip(), p[3].strip()
                for g, want in ((state + place.zfill(5), want_place),
                                (state + county.zfill(3) + mcd.zfill(5), want_cousub)):
                    if g in want and (g, year) not in seen and place != "00000":
                        out.append({"geoid": g, "year": year, "bps_units": units})
                        seen.add((g, year))
    print("  BPS: %d jurisdiction-years" % len(out))
    return pd.DataFrame(out)


def bps_series(acs_panel_df, bps_df):
    """Base-year ACS stock carried forward by cumulated BPS permits."""
    base_y = min(UNIT_YEARS)
    base = acs_panel_df[acs_panel_df.year == base_y].set_index("geoid").units
    wide = bps_df.pivot(index="geoid", columns="year", values="bps_units").fillna(0)
    rows = []
    for g, stock in base.items():
        rows.append({"geoid": g, "year": base_y, "units": stock})
        for y in range(base_y + 1, max(UNIT_YEARS) + 1):
            stock = stock + (wide.at[g, y] if (g in wide.index and y in wide.columns) else 0)
            rows.append({"geoid": g, "year": y, "units": stock})
    return pd.DataFrame(rows)


def apply_carveouts(panel, fallback):
    """Reduce each urban county to its FUNDED area.

    An urban county's CDBG award covers only the parts of the county whose
    municipalities are not separately funded. f_inC is the share of a carved
    municipality lying inside the county, so
        funded(C, t) = county(C, t) - sum_u f_u * units_u(t)
    Uses the same series for the carved municipality where available, else ACS.
    """
    path = os.path.join(INPUTS, "urban_county_carveouts.csv")
    if not os.path.exists(path):
        return panel, 0
    carve = pd.read_csv(path, dtype={"county_geoid": str, "uglg_geoid": str})
    touched = 0
    for county, grp in carve.groupby("county_geoid"):
        if county not in panel:
            continue
        for year in list(panel[county]):
            sub = 0.0
            for u in grp.itertuples():
                units = panel.get(u.uglg_geoid, {}).get(year)
                if units is None:
                    units = fallback.get(u.uglg_geoid, {}).get(year)
                if units:
                    sub += float(u.f_inC) * units
            panel[county][year] = max(panel[county][year] - sub, 0.0)
        touched += 1
    return panel, touched


def rolling_exemption_rate(decl, counties, first=2017, last=2026):
    """Share of recipients the disaster screen would exempt, year by year.

    COVID-19 is the reason this is reported with and without an exclusion: it
    was declared for effectively every county in the country in 2020, so any
    3-year window containing March 2020 exempts almost everyone. County-level
    COVID designations run 13 March to 17 April 2020, so three anchors are
    affected, leaving seven clean windows: 2017-2019 and 2023-2026.
    """
    print("\nSCREEN (C) EXEMPTION RATE, rolling 3-year windows, 1 May anchors")
    clean, covid_rows = [], []
    for y in range(first, last + 1):
        end = pd.Timestamp("%d-05-01" % y)
        desig = designated_in(decl, end)
        n = sum(1 for s in counties.values() if s & desig)
        pct = 100.0 * n / len(counties)
        window_has_covid = bool(len(decl[(decl.date > end - pd.DateOffset(years=3))
                                         & (decl.date <= end)
                                         & decl.incidentType.eq("Biological")]))
        (covid_rows if window_has_covid else clean).append(pct)
        print("   %d: %4d of %d = %5.1f%%%s"
              % (y, n, len(counties), pct, "   <- window contains COVID-19"
                 if window_has_covid else ""))
    allr = clean + covid_rows
    print("   mean over all windows            %5.1f%%" % (sum(allr) / len(allr)))
    if clean:
        print("   mean over the %d non-COVID windows %5.1f%%"
              % (len(clean), sum(clean) / len(clean)))


def permits_after_disasters(decl, counties, bps, panel, skip=()):
    """Do jurisdictions build less after a disaster? Compare permits either side.

    Events are qualifying declarations in 2018-2021, so three years of permits
    exist on both sides. COVID-19 is excluded: it covered every county, which
    would leave no comparison group at all. Permits are de-meaned by year, so
    the national construction cycle is removed and this is relative performance.
    Urban counties are dropped: their BPS permits cover the whole county while
    the unit panel counts only the funded area, which would inflate their rate.
    Descriptive only -- disasters are not randomly assigned, and undeclared
    jurisdictions are scored against a fixed 2019 event year.
    """
    print("\nPERMITS BEFORE AND AFTER A DISASTER DECLARATION")
    if not len(bps):
        print("   (no BPS data)")
        return
    d = bps[~bps.geoid.isin(skip)].copy()
    d["units"] = [panel.get(g, {}).get(y) for g, y in zip(d.geoid, d.year)]
    d = d[d.units.notna() & (d.units > 0)]
    d["rate"] = 1000.0 * d.bps_units / d.units
    d["rate_dm"] = d.rate - d.groupby("year").rate.transform("mean")
    wide = d.pivot_table(index="geoid", columns="year", values="rate_dm")

    ev = decl[decl.date.dt.year.between(2018, 2021) & ~decl.incidentType.eq("Biological")]
    by_county = ev.groupby("cg").date.min().dt.year.to_dict()
    rows = []
    for g, cs in counties.items():
        if g not in wide.index or g in skip:
            continue
        years = [by_county[c] for c in cs if c in by_county]
        t, declared = (min(years), True) if years else (2019, False)
        pre = [wide.at[g, y] for y in range(t - 3, t)
               if y in wide.columns and pd.notna(wide.at[g, y])]
        post = [wide.at[g, y] for y in range(t + 1, t + 4)
                if y in wide.columns and pd.notna(wide.at[g, y])]
        if len(pre) < 3 or len(post) < 3:
            continue
        rows.append({"declared": declared,
                     "delta": sum(post) / len(post) - sum(pre) / len(pre)})
    r = pd.DataFrame(rows)
    if r.empty:
        print("   (not enough overlapping permit years)")
        return
    print("   %-16s %5s %9s %14s" % ("group", "n", "change", "built more after"))
    for flag, lab in ((True, "declared"), (False, "no declaration")):
        s = r[r.declared == flag]
        if len(s):
            print("   %-16s %5d %9.3f %13.0f%%"
                  % (lab, len(s), s.delta.mean(), 100 * (s.delta > 0).mean()))
    a, b = r[r.declared].delta, r[~r.declared].delta
    if len(a) > 1 and len(b) > 1:
        diff = a.mean() - b.mean()
        try:
            from scipy import stats
            p = stats.ttest_ind(a, b, equal_var=False)[1]
            print("   difference %+.3f permits per 1,000 units, p = %.3f" % (diff, p))
        except ImportError:
            print("   difference %+.3f permits per 1,000 units" % diff)
    print("   (units: permits per 1,000 housing units per year, de-meaned by year)")


# ---------------------------------------------------------------------------
# The statute
# ---------------------------------------------------------------------------
def growth_rate(panel, geoid, y0, y1):
    """Average annual percentage increase between two fiscal-year endpoints."""
    s = panel.get(geoid)
    if not s:
        return None
    a, b = s.get(y0), s.get(y1)
    if not a or not b or a <= 0 or b <= 0:
        return None
    return (b / a) ** (1.0 / (y1 - y0)) - 1.0


def hgir(current, prior):
    """(a)(5): (current - prior) / (|current| + |prior|).

    Note this saturates: whenever the two rates have opposite signs the
    numerator and denominator are equal in magnitude and the result is exactly
    +/-1, however small either rate is.
    """
    if current is None or prior is None:
        return None
    denom = abs(current) + abs(prior)
    return None if denom == 0 else (current - prior) / denom


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--census-key", default=os.environ.get("CENSUS_API_KEY", ""))
    ap.add_argument("--vacancy-rate", type=float, default=NATIONAL_RENTAL_VACANCY_RATE,
                    help="national annual rental vacancy rate for screen (B)")
    ap.add_argument("--zoning-rule", default=ZONING_RULE, choices=["none", "counties"],
                    help="screen (D): 'none' (baseline) or 'counties'")
    ap.add_argument("--series", default="acs", choices=["acs", "bps"],
                    help="housing-unit series; the ACL cannot span the windows")
    ap.add_argument("--extras", action="store_true",
                    help="also report the rolling exemption rate and the "
                         "before/after permit comparison")
    args = ap.parse_args()
    if not args.census_key:
        sys.exit("need a Census API key: --census-key or CENSUS_API_KEY")
    os.makedirs(OUTPUTS, exist_ok=True)

    jur = pd.read_csv(os.path.join(INPUTS, "jurisdictions.csv"), dtype={"geoid": str})
    xwalk = pd.read_csv(os.path.join(INPUTS, "jurisdiction_counties.csv"),
                        dtype={"geoid": str, "county_geoid": str})
    safmr = pd.read_csv(os.path.join(INPUTS, "safmr_by_jurisdiction.csv"),
                        dtype={"geoid": str}).set_index("geoid").safmr_median
    counties = xwalk.groupby("geoid").county_geoid.apply(set).to_dict()
    states = sorted({g[:2] for g in jur.geoid})
    by_type = {t: set(d.geoid) for t, d in jur.groupby("geoid_type")}
    cousub_states = sorted({g[:2] for g in by_type.get("cousub", set())})
    print("universe: %d recipients, $%s FY2025 CDBG"
          % (len(jur), format(jur.cdbg_fy25.sum(), ",.0f")))

    print("[1/5] ACS housing-unit panel")
    acs_df = acs_panel(states, cousub_states, args.census_key)
    def to_dict(df):
        return {g: dict(zip(d.year, d.units))
                for g, d in df.groupby("geoid")[["year", "units"]]}
    acs_lookup = to_dict(acs_df)
    print("[2/5] ACS home value and rental vacancy")
    elig, us_home = acs_eligibility(states, cousub_states, args.census_key)
    elig = elig.set_index("geoid")
    print("      national median home value: $%s" % format(us_home, ",.0f"))
    print("[3/5] FEMA declarations")
    # pulled wide so the same rows serve the screen and the two extras below
    decl = fema_declarations(pd.Timestamp("2014-01-01"), pd.Timestamp(ALLOCATION_DATE)
                             if not args.extras else pd.Timestamp.today())
    designated = designated_in(decl, pd.Timestamp(ALLOCATION_DATE))
    print("      screen (C) window: %d counties designated"
          % len(designated))
    print("[4/5] Building Permits Survey (comparison series)")
    bps = bps_permits(by_type.get("place", set()), by_type.get("county", set()),
                      by_type.get("cousub", set()))
    bps_latest = (bps[bps.year == UNIT_YEARS[-1]].set_index("geoid").bps_units
                  if len(bps) else pd.Series(dtype=float))

    # choose the housing-unit series, then reduce urban counties to funded area
    panel = to_dict(bps_series(acs_df, bps)) if args.series == "bps" else acs_lookup
    panel, n_carved = apply_carveouts(panel, acs_lookup)
    print("      series=%s; %d urban counties reduced to their funded area"
          % (args.series, n_carved))

    print("[5/5] screens, rates and reallocation")
    safmr_60 = safmr.quantile(SAFMR_PERCENTILE / 100.0)
    rows = []
    for r in jur.itertuples():
        hv = elig.home_value.get(r.geoid)
        rvr = elig.rental_vacancy_rate.get(r.geoid)
        sm = safmr.get(r.geoid)

        # (a)(3)(A) low cost: BOTH rent at/below the 60th percentile AND home
        # value below the national median.
        a = bool(pd.notna(sm) and pd.notna(hv) and sm <= safmr_60 and hv < us_home)
        # (a)(3)(B) vacancy above the national annual rate
        b = bool(pd.notna(rvr) and rvr > args.vacancy_rate)
        # (a)(3)(C) ANY county the jurisdiction touches was designated. 44 CFR
        # 206.40(b) says a designated area includes all local governments within
        # its boundaries, which is why the test is any-overlap rather than all.
        c = bool(counties.get(r.geoid, set()) & designated)
        # (a)(3)(D) see ZONING_RULE above
        d = bool(args.zoning_rule == "counties" and r.geoid_type == "county")

        cur = growth_rate(panel, r.geoid, *CURRENT_WINDOW)
        pri = growth_rate(panel, r.geoid, *PRIOR_WINDOW)
        units_now = panel.get(r.geoid, {}).get(CURRENT_WINDOW[1])
        units_prev = panel.get(r.geoid, {}).get(CURRENT_WINDOW[1] - 1)
        rows.append({
            "geoid": r.geoid, "name": r.name, "state": r.state, "type": r.type,
            "cdbg_fy25": r.cdbg_fy25,
            "exempt_A": a, "exempt_B": b, "exempt_C": c, "exempt_D": d,
            "eligible": not (a or b or c or d),
            "current_rate": cur, "prior_rate": pri, "hgir": hgir(cur, pri),
            # (b)(2)(A)(ii)(II) weights each bonus by the change in units in the
            # most recent year. The statute sets no floor on this quantity.
            "recent_change": (None if units_now is None or units_prev is None
                              else units_now - units_prev),
            "bps_permits_latest": bps_latest.get(r.geoid),
        })
    out = pd.DataFrame(rows)

    # (b)(2): the median is taken over ELIGIBLE recipients other than extremely
    # high-growth ones. Excluded recipients therefore set the benchmark that
    # scores everyone else.
    pool = out[out.eligible & out.hgir.notna()]
    scored = pool[~(pool.current_rate >= EXTREME_GROWTH_RATE)]
    median = scored.hgir.median()
    out["extreme"] = out.current_rate >= EXTREME_GROWTH_RATE
    out["bna_class"] = None
    penal = out.eligible & out.hgir.notna() & ~out.extreme & (out.hgir < median)
    bonus = out.eligible & out.hgir.notna() & ~penal
    out.loc[penal, "bna_class"] = "penalty"
    out.loc[bonus, "bna_class"] = "bonus"

    out["adjustment"] = 0.0
    out.loc[penal, "adjustment"] = -PENALTY_RATE * out.loc[penal, "cdbg_fy25"]
    total_pool = -out.loc[penal, "adjustment"].sum()
    # bonuses are shares of that fixed pool, in proportion to units added
    weight = out.recent_change.where(bonus, 0).clip(lower=0).fillna(0)
    if weight.sum() > 0:
        out.loc[bonus, "adjustment"] = total_pool * weight[bonus] / weight.sum()
    out["adjusted_cdbg"] = out.cdbg_fy25 + out.adjustment
    out.to_csv(os.path.join(OUTPUTS, "results.csv"), index=False, encoding="utf-8")

    n = len(out)
    ex = n - int(out.eligible.sum())
    print()
    print("median HGIR among eligible, non-extreme recipients: %+.4f" % median)
    print("exempt: %d of %d (%.1f%%)   eligible: %d"
          % (ex, n, 100 * ex / n, int(out.eligible.sum())))
    for s in "ABCD":
        col = out["exempt_%s" % s]
        print("   screen %s: %4d (%.1f%%)" % (s, col.sum(), 100 * col.mean()))
    print("penalised %d, bonus %d" % (penal.sum(), bonus.sum()))
    print("redistributed $%s = %.3f%% of $%s"
          % (format(total_pool, ",.0f"), 100 * total_pool / out.cdbg_fy25.sum(),
             format(out.cdbg_fy25.sum(), ",.0f")))
    per = total_pool / weight[bonus].sum() if weight[bonus].sum() else float("nan")
    print("bonus per net home added: $%.2f" % per)
    paid = int((out.adjustment > 0).sum())
    print("   %d bonus recipients receive money; %d receive $0 because their most "
          "recent year's change was not positive" % (paid, int(bonus.sum()) - paid))

    # Per MARGINAL home. The Act qualifies a recipient on a five-year rate
    # comparison but pays on one year's units, so this prorates the payment year
    # by the share of its growth rate above the rate needed to reach the median.
    def marginal(row):
        c, p = row.current_rate, row.prior_rate
        if pd.isna(c) or pd.isna(p) or c <= 0 or row.adjustment <= 0:
            return None, None
        needed = (median * abs(p) + p) / (1 - median)
        homes = row.recent_change * (c - needed) / c
        return homes, (row.adjustment / homes if homes and homes > 0 else None)
    print("   largest recipients, cost per MARGINAL home above the median:")
    for row in out[out.adjustment > 0].nlargest(5, "adjustment").itertuples():
        homes, cost = marginal(row)
        if cost:
            print("     %-20s %-2s $%9s for %6.0f homes, %5.0f marginal = $%.0f each"
                  % (str(row.name)[:20], row.state, format(row.adjustment, ",.0f"),
                     row.recent_change, homes, cost))
    if args.extras:
        rolling_exemption_rate(decl, counties)
        permits_after_disasters(decl, counties, bps, panel,
                                skip=by_type.get("county", set()))

    print()
    print("wrote outputs/results.csv")


if __name__ == "__main__":
    main()
