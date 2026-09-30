"""Do natural disaster declarations destroy homes? Replication.

Emits three regressions:

  1  ACS 1-year housing stock, all declarations          local projections
  2  ACS 1-year housing stock, by disaster type          local projections
  3  ACL address growth over the statutory window        cross-section

Usage:
    python disasters.py --census-key YOUR_CENSUS_API_KEY

Everything downloaded is cached under cache/; later runs are offline. The ACL
step is the slow one: it pulls block-level address counts for every state at
eight snapshots, plus the 2020 block assignment files, and takes upwards of an
hour on a first run. Skip it with --no-acl.

Heterogeneity by local housing supply elasticity is not estimated here. Roth
Tran and Wilson (2024) establish that result directly, on county prices and
population, with a panel long enough to support it.
"""
import argparse
import io
import os
import re
import sys
import zipfile
from collections import defaultdict

import numpy as np
import pandas as pd

import build_now as B

ACL_BASE = "https://www2.census.gov/geo/pvs/addcountlisting/"
BAF_BASE = "https://www2.census.gov/geo/docs/maps-data/data/baf2020/"
ACL_SNAPSHOTS = [(2020, "062022", "2022-06"), (2023, "072023", "2023-07"),
                 (2023, "122023", "2023-12"), (2024, "072024", "2024-07"),
                 (2024, "122024", "2024-12"), (2025, "072025", "2025-07"),
                 (2025, "122025", "2025-12")]
WIN_LO, WIN_HI = pd.Timestamp("2022-05-01"), pd.Timestamp("2025-05-01")

GROUPS = {"Hurricane/tropical": {"Hurricane", "Tropical Storm", "Typhoon",
                                 "Tropical Depression", "Coastal Storm"},
          "Flood": {"Flood"}, "Fire": {"Fire"},
          "Winter": {"Snowstorm", "Severe Ice Storm", "Winter Storm", "Freezing"},
          "Severe storm/tornado": {"Severe Storm", "Tornado",
                                   "Straight-Line Winds"}}
ORDER = ["All declarations", "Individual Assistance"] + list(GROUPS)
HORIZONS = [-3, -2, 0, 1, 2, 3]
ACS1_YEARS = [y for y in range(2010, 2025) if y != 2020]


# --------------------------------------------------------------- geography ---
def county_sets():
    xw = pd.read_csv(os.path.join(B.INPUTS, "jurisdiction_counties.csv"),
                     dtype={"geoid": str, "county_geoid": str})
    return xw.groupby("geoid").county_geoid.apply(set).to_dict()


def treatment_maps(geoids, decl):
    """{definition: {geoid: set(years)}}; treated if ANY county is designated."""
    cs = county_sets()
    defs = {"All declarations": decl, "Individual Assistance": decl[decl.ih]}
    for lab, types in GROUPS.items():
        defs[lab] = decl[decl.incidentType.isin(types)]
    out = {}
    for lab in ORDER:
        by_cty = defs[lab].groupby("cg").year.apply(set).to_dict()
        out[lab] = {g: set().union(*[by_cty.get(c, set())
                                     for c in cs.get(g, {None})] or [set()])
                    for g in geoids}
    return out


def declarations():
    d = B.fema_declarations(pd.Timestamp("1995-01-01"), pd.Timestamp.today())
    d = d[~d.incidentType.eq("Biological")].copy()
    d["year"] = d.date.dt.year
    return d


# -------------------------------------------------------------- ACS 1-year ---
def acs1(year, variables, geo, key):
    url = ("https://api.census.gov/data/%d/acs/acs1?get=%s&for=%s&key=%s"
           % (year, ",".join(variables), geo, key))
    tag = ("acs1_%d_%s_%s.json"
           % (year, "_".join(variables),
              geo.replace(":", "").replace("*", "all").replace("&in=", "_")))
    rows = __import__("json").loads(B.fetch(url, tag))
    head, body = rows[0], rows[1:]
    out = []
    for r in body:
        d = dict(zip(head, r))
        out.append({"geoid": "".join(d[c] for c in head[len(variables):]),
                    **{v: pd.to_numeric(d[v], errors="coerce")
                       for v in variables}})
    return pd.DataFrame(out)


def decennial_2020(states, cousub_states, key):
    """H1_001N, to fill the year the 1-year programme did not publish."""
    frames = []
    for scope in B.acs_calls(states, cousub_states, ["H1_001N"]):
        url = ("https://api.census.gov/data/2020/dec/dhc?get=H1_001N&for=%s&key=%s"
               % (scope, key))
        tag = "dec2020_%s.json" % scope.replace(":", "").replace(
            "*", "all").replace("&in=", "_")
        try:
            rows = __import__("json").loads(B.fetch(url, tag))
        except Exception:
            continue
        head, body = rows[0], rows[1:]
        for r in body:
            d = dict(zip(head, r))
            frames.append({"geoid": "".join(d[c] for c in head[1:]),
                           "units": pd.to_numeric(d["H1_001N"], errors="coerce"),
                           "year": 2020})
    return pd.DataFrame(frames)


def acs1_stock(key, minyears=12):
    jur = pd.read_csv(os.path.join(B.INPUTS, "jurisdictions.csv"),
                      dtype={"geoid": str})
    keep = set(jur.geoid)
    states = sorted({g[:2] for g in jur.geoid})
    cousub = sorted({g[:2] for g in jur.geoid[jur.geoid_type == "cousub"]})
    frames = []
    for year in ACS1_YEARS:
        got = []
        for scope in B.acs_calls(states, cousub, ["B25001_001E"]):
            try:
                got.append(acs1(year, ["B25001_001E"], scope, key))
            except Exception:
                pass
        if not got:
            continue
        d = pd.concat(got, ignore_index=True).drop_duplicates("geoid")
        d = d.rename(columns={"B25001_001E": "units"})
        d["year"] = year
        frames.append(d[["geoid", "year", "units"]])
        print("  ACS 1-year %d" % year, flush=True)
    dec = decennial_2020(states, cousub, key)
    if len(dec):
        frames.append(dec[["geoid", "year", "units"]])
    p = pd.concat(frames, ignore_index=True)
    p = p[p.geoid.isin(keep) & (p.units > 0)]

    ser = {}
    for g, sub in p.groupby("geoid"):
        sub = sub.sort_values("year")
        if len(sub) < minyears:
            continue
        lv = np.log(sub.units.astype(float))
        base = float(lv.iloc[0])
        ser[g] = {int(y): 100.0 * (float(x) - base)
                  for y, x in zip(sub.year, lv)}
    return ser


# --------------------------------------------------------------------- ACL ---
def _acl_listing(folder):
    html = B.fetch(ACL_BASE + "%d/" % folder,
                   "acl_index_%d.html" % folder).decode("latin-1") \
        if isinstance(B.fetch(ACL_BASE + "%d/" % folder,
                              "acl_index_%d.html" % folder), bytes) \
        else B.fetch(ACL_BASE + "%d/" % folder, "acl_index_%d.html" % folder)
    out = defaultdict(dict)
    for fn in re.findall(
            r'href="(\d\d_[A-Za-z_]+_AddressBlockCountList_\d{6}\.txt)"', html):
        out[fn.split("_")[-1][:6]][fn[:2]] = fn
    return out


def _baf_maps(st, usps, want_place, want_cousub):
    """Block -> place and block -> county subdivision, from the 2020 BAF.

    The file is keyed by FIPS and postal abbreviation both: ST01_AL, not
    ST01_01. Getting that wrong 404s, and without these maps only the
    county-type recipients survive -- a quarter of the sample, silently.
    """
    try:
        raw = B.fetch(BAF_BASE + "BlockAssign_ST%s_%s.zip" % (st, usps),
                      "baf_%s.zip" % st, binary=True)
    except Exception as e:
        print("  WARNING: no BAF for %s (%s): %s" % (usps, st, e), flush=True)
        return {}, {}
    zf = zipfile.ZipFile(io.BytesIO(raw))
    b2p, b2c = {}, {}
    for nm in zf.namelist():
        if nm.endswith("_INCPLACE_CDP.txt"):
            for line in io.StringIO(zf.read(nm).decode("latin-1")):
                p = line.rstrip("\n").split("|")
                if len(p) >= 2 and p[1] and st + p[1] in want_place:
                    b2p[p[0]] = st + p[1]
        elif nm.endswith("_MCD.txt"):
            for line in io.StringIO(zf.read(nm).decode("latin-1")):
                p = line.rstrip("\n").split("|")
                if len(p) >= 3 and p[2] and st + p[1] + p[2] in want_cousub:
                    b2c[p[0]] = st + p[1] + p[2]
    return b2p, b2c


def acl_panel():
    jur = pd.read_csv(os.path.join(B.INPUTS, "jurisdictions.csv"),
                      dtype={"geoid": str})
    want_place = set(jur.geoid[jur.geoid_type == "place"])
    want_cousub = set(jur.geoid[jur.geoid_type == "cousub"])
    want_county = set(jur.geoid[jur.geoid_type == "county"])
    states = sorted({g[:2] for g in jur.geoid})
    usps = jur.assign(f=jur.geoid.str[:2]).groupby("f").state.first().to_dict()

    maps = {st: _baf_maps(st, usps[st], want_place, want_cousub)
            for st in states}
    covered = sum(1 for st in states if maps[st][0] or maps[st][1])
    print("  BAF crosswalks built for %d of %d states" % (covered, len(states)),
          flush=True)
    rows = []
    for folder, stamp, label in ACL_SNAPSHOTS:
        listing = _acl_listing(folder).get(stamp, {})
        for st in states:
            fn = listing.get(st)
            if not fn:
                continue
            try:
                txt = B.fetch(ACL_BASE + "%d/%s" % (folder, fn),
                              "acl_%s_%s.txt" % (st, stamp))
            except Exception:
                continue
            b2p, b2c = maps.get(st, ({}, {}))
            acc = defaultdict(float)
            for line in io.StringIO(txt):
                p = line.rstrip("\n").split("|")
                if len(p) < 6 or not p[4].isdigit() or len(p[4]) != 15:
                    continue
                try:
                    n = float(p[5])
                except ValueError:
                    continue
                blk = p[4]
                cty = blk[:5]
                if cty in want_county:
                    acc[cty] += n
                g = b2p.get(blk)
                if g:
                    acc[g] += n
                g = b2c.get(blk)
                if g:
                    acc[g] += n
            for g, v in acc.items():
                rows.append({"geoid": g, "snapshot": label, "units": v})
        print("  ACL %s" % label, flush=True)
    d = pd.DataFrame(rows).groupby(["geoid", "snapshot"], as_index=False).units.sum()
    p = d.pivot(index="geoid", columns="snapshot", values="units")
    # Only the two endpoints enter the regression, so requiring all seven
    # snapshots would discard recipients the estimate can actually use.
    return p.dropna(subset=[ACL_SNAPSHOTS[0][2], ACL_SNAPSHOTS[-1][2]])


# -------------------------------------------------------------- estimation ---
def _sweep(M, codes, ng):
    cnt = np.bincount(codes, minlength=ng).astype(float)
    cnt[cnt == 0] = 1.0
    for j in range(M.shape[1]):
        s = np.bincount(codes, weights=M[:, j], minlength=ng)
        M[:, j] -= (s / cnt)[codes]
    return M


def _demean(df, cols, g1, g2, tol=1e-11, maxit=400):
    M = df[cols].astype(float).to_numpy().copy()
    c1, u1 = pd.factorize(df[g1].to_numpy())
    c2, u2 = pd.factorize(df[g2].to_numpy())
    for _ in range(maxit):
        prev = M.copy()
        M = _sweep(M, c1, len(u1))
        M = _sweep(M, c2, len(u2))
        if np.max(np.abs(M - prev)) < tol:
            break
    return pd.DataFrame(M, columns=cols, index=df.index), len(u1), len(u2)


def _meat(X, r, codes):
    m = np.zeros((X.shape[1], X.shape[1]))
    for g in np.unique(codes):
        s = codes == g
        u = X[s].T @ r[s]
        m += np.outer(u, u)
    return m


def fit(d, y, xs, unit="geoid", time="rt", cl2="st_year"):
    """Two-way FE with Cameron-Gelbach-Miller two-way clustering."""
    dm, n1, n2 = _demean(d, [y] + xs, unit, time)
    Y, X = dm[y].to_numpy(), dm[xs].to_numpy()
    if np.linalg.matrix_rank(X) < X.shape[1]:
        keep = []
        for j in range(X.shape[1]):
            if np.linalg.matrix_rank(X[:, keep + [j]]) == len(keep) + 1:
                keep.append(j)
        X, xs = X[:, keep], [xs[j] for j in keep]
    inv = np.linalg.inv(X.T @ X)
    beta = inv @ (X.T @ Y)
    resid = Y - X @ beta
    c1 = pd.factorize(d[unit].to_numpy())[0]
    c2 = pd.factorize(d[cl2].to_numpy())[0]
    c12 = pd.factorize(d[unit].astype(str) + "|" + d[cl2].astype(str))[0]
    V = inv @ (_meat(X, resid, c1) + _meat(X, resid, c2)
               - _meat(X, resid, c12)) @ inv
    G, (n, k) = len(np.unique(c1)), X.shape
    V *= (G / max(G - 1.0, 1.0)) * ((n - 1.0) / max(n - k - (n1 + n2 - 1), 1.0))
    w, Q = np.linalg.eigh((V + V.T) / 2.0)
    if np.any(w < 0):
        V = Q @ np.diag(np.clip(w, 0, None)) @ Q.T
    return dict(zip(xs, zip(beta, np.sqrt(np.clip(np.diag(V), 0, None))))), n


NE = {"09", "23", "25", "33", "34", "36", "42", "44", "50"}
MW = {"17", "18", "19", "20", "26", "27", "29", "31", "38", "39", "46", "55"}
WE = {"02", "04", "06", "08", "15", "16", "30", "32", "35", "41", "49", "53", "56"}


def region(st):
    return "NE" if st in NE else "MW" if st in MW else "WE" if st in WE else "SO"


def lp(ser, dmap, horizons=HORIZONS, p=3):
    """Local projections difference-in-differences."""
    years = sorted({t for s in ser.values() for t in s})
    ylo, yhi, hmax = min(years), max(years), max(horizons)
    taus = [t for t in range(-p, hmax + 1) if t != 0]
    rows = []
    for h in horizons:
        if h == -1:
            continue
        recs = []
        for g, s in ser.items():
            dec = dmap.get(g, set())
            for t in years:
                if t + max(h, 0) > yhi or t + min(h, 0) < ylo:
                    continue
                if t not in s or (t + h) not in s or (t - 1) not in s:
                    continue
                r = {"geoid": g, "dep": s[t + h] - s[t - 1],
                     "D0": float(t in dec), "rt": region(g[:2]) + "_" + str(t),
                     "st_year": g[:2] + "_" + str(t)}
                for tau in taus:
                    r["D%+d" % tau] = (float((t + tau) in dec)
                                       if ylo <= t + tau <= yhi else 0.0)
                recs.append(r)
        d = pd.DataFrame(recs)
        if len(d) < 300:
            continue
        rest = sorted(c for c in d.columns
                      if c.startswith("D") and c != "D0")
        out, n = fit(d, "dep", ["D0"] + rest)
        if "D0" not in out:
            # fit() drops collinear columns; on a short panel D0 can be one of
            # them, and the horizon is then not identified rather than zero.
            print("  h=%+d: treatment collinear after fixed effects, skipped"
                  % h, flush=True)
            continue
        b, se = out["D0"]
        rows.append({"h": h, "n": n, "beta": b, "beta_se": se})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ output ---
def show(df, title, col="beta"):
    print("\n" + title)
    print("  %-5s %10s %10s %8s %10s" % ("h", "beta", "se", "t", "n"))
    for r in df.itertuples():
        b, s = getattr(r, col), getattr(r, col + "_se")
        star = "**" if abs(b / s) > 1.96 else "*" if abs(b / s) > 1.65 else ""
        print("  %-5d %10.3f %10.3f %8.2f %10d %s" % (r.h, b, s, b / s, r.n, star))


def fe_ols(y, X, fe, clus):
    y, X = np.asarray(y, float), np.atleast_2d(np.asarray(X, float))
    if X.shape[0] != len(y):
        X = X.T
    codes = pd.factorize(np.asarray(fe))[0]
    M = np.column_stack([y, X])
    cnt = np.bincount(codes).astype(float)
    cnt[cnt == 0] = 1.0
    for j in range(M.shape[1]):
        s = np.bincount(codes, weights=M[:, j], minlength=len(cnt))
        M[:, j] -= (s / cnt)[codes]
    keep = np.all(np.isfinite(M), axis=1)
    Y, Xd, cl = M[keep, 0], M[keep, 1:], np.asarray(clus)[keep]
    inv = np.linalg.inv(Xd.T @ Xd)
    beta = inv @ (Xd.T @ Y)
    r = Y - Xd @ beta
    cc = pd.factorize(cl)[0]
    meat = np.zeros((Xd.shape[1], Xd.shape[1]))
    for g in np.unique(cc):
        s = cc == g
        u = Xd[s].T @ r[s]
        meat += np.outer(u, u)
    V = inv @ meat @ inv
    G, n, k = len(np.unique(cc)), len(Y), Xd.shape[1]
    V *= (G / max(G - 1.0, 1.0)) * ((n - 1.0) / max(n - k - len(cnt), 1.0))
    return beta, np.sqrt(np.clip(np.diag(V), 0, None))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--census-key", required=True)
    ap.add_argument("--no-acl", action="store_true")
    a = ap.parse_args()
    os.makedirs(B.CACHE, exist_ok=True)

    decl = declarations()
    print("\n[1/3] ACS 1-year housing stock")
    ser = acs1_stock(a.census_key)
    maps = treatment_maps(list(ser), decl)
    yrs = sorted({y for s in ser.values() for y in s})
    print("  %d recipients, %d-%d" % (len(ser), min(yrs), max(yrs)))
    show(lp(ser, maps["All declarations"]),
         "REGRESSION 1  ACS 1-year stock, all declarations "
         "(100 x log units, cumulative vs t-1)")

    print("\n[2/3] by disaster type")
    print("\nREGRESSION 2  ACS 1-year stock, by disaster type")
    print("  %-24s %s" % ("", "  ".join("%8s" % ("h=%d" % h)
                                        for h in HORIZONS)))
    for lab in ORDER:
        r = lp(ser, maps[lab]).set_index("h")
        cells = []
        for h in HORIZONS:
            if h not in r.index:
                cells.append("%8s" % "-")
                continue
            b, s = r.beta[h], r.beta_se[h]
            cells.append("%8s" % ("%+.3f%s" % (b, "**" if abs(b / s) > 1.96
                                               else "*" if abs(b / s) > 1.65
                                               else "")))
        print("  %-24s %s" % (lab, "  ".join(cells)))

    if not a.no_acl:
        print("\n[3/3] ACL address growth")
        w = acl_panel()
        cs = county_sets()
        w = w[[g in cs for g in w.index]]
        g = 100 * ((w["2025-12"] / w["2022-06"]) ** (1 / 3.5) - 1)
        g = g.replace([np.inf, -np.inf], np.nan).dropna()
        st = pd.Series({i: i[:2] for i in g.index})
        clus = pd.Series({i: sorted(cs[i])[0] for i in g.index})
        print("\nREGRESSION 3  ACL annualized address growth 2022-06 to 2025-12,")
        print("              state fixed effects, clustered on county")
        print("  %-24s %6s %8s %10s %10s %9s %7s"
              % ("designated by", "n", "states", "treated", "untreated",
                 "beta", "t"))
        defs = {"All declarations": decl, "Individual Assistance": decl[decl.ih]}
        for lab, types in GROUPS.items():
            defs[lab] = decl[decl.incidentType.isin(types)]
        for lab in ORDER:
            sub = defs[lab]
            cty = set(sub.cg[(sub.date >= WIN_LO) & (sub.date <= WIN_HI)])
            t = pd.Series({i: float(bool(cs[i] & cty)) for i in g.index})
            if t.sum() < 15 or (1 - t).sum() < 15:
                continue
            b, se = fe_ols(g.values, t.values.reshape(-1, 1), st.values,
                           clus.values)
            star = "**" if abs(b[0] / se[0]) > 1.96 else ""
            print("  %-24s %6d %8d %9.3f%% %9.3f%% %+9.3f %6.2f %s"
                  % (lab, int(t.sum()), len({i[:2] for i in g.index if t[i] == 1}),
                     g[t == 1].mean(), g[t == 0].mean(), b[0], b[0] / se[0], star))


if __name__ == "__main__":
    sys.exit(main())
