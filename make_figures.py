"""Build the Datawrapper-ready CSVs for the article's figures.

Run after build_now.py. Reads outputs/results.csv and writes four files to
figures/, each ready to paste or upload into Datawrapper.

    fig1_net_homes.csv   bar: where the housing actually gets built
    fig2_cdbg.csv        bar: CDBG by category, and how little of it moves
    fig3_per_home.csv    bar: the incentive rate, per net home added
    fig4_map.csv         symbol map: every recipient, coloured by outcome

Colour key used throughout (set these in the Datawrapper UI):
    exempt, disaster        #8A7458  brown
    exempt, other screens   #9A9A93  grey
    gains funding           #3F7D55  green
    loses funding           #A63A32  red
    untouched               #C9C6BC  light grey
"""
import os

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "figures")
RESULTS = os.path.join(HERE, "outputs", "results.csv")
CENTROIDS = os.path.join(HERE, "..", "buildnow_model", "data_raw", "hud",
                         "uglg_centroids.csv")

COLOURS = {"Exempt — recent disaster": "#8A7458",
           "Exempt — other screens": "#9A9A93",
           "Gains funding": "#3F7D55",
           "Loses funding": "#A63A32"}
ORDER = list(COLOURS)


def categorise(r):
    """Four buckets. Bonus recipients paid $0 stay with the bonus group: they
    are the same statutory category, and merging keeps every bar positive."""
    if r.eligible and r.bna_class == "bonus":
        return "Gains funding"
    if r.eligible and r.bna_class == "penalty":
        return "Loses funding"
    if r.exempt_C:
        return "Exempt — recent disaster"
    return "Exempt — other screens"


def main():
    os.makedirs(OUT, exist_ok=True)
    d = pd.read_csv(RESULTS, dtype={"geoid": str})
    d["category"] = d.apply(categorise, axis=1)
    total_net = d.recent_change.sum()
    total_cdbg = d.cdbg_fy25.sum()
    moved = -d.loc[d.bna_class.eq("penalty"), "adjustment"].sum()

    g = d.groupby("category").agg(recipients=("geoid", "size"),
                                  cdbg=("cdbg_fy25", "sum"),
                                  net_homes=("recent_change", "sum"),
                                  adjustment=("adjustment", "sum")).reindex(ORDER)

    # --- fig 1: where the housing actually gets built ----------------------
    f1 = pd.DataFrame({
        "Category": g.index,
        "Share of net homes added (%)": (100 * g.net_homes / total_net).round(1),
        "Net homes added": g.net_homes.round(0).astype(int),
        "Recipients": g.recipients.values,
        "Colour": [COLOURS[c] for c in g.index]})
    f1.to_csv(os.path.join(OUT, "fig1_net_homes.csv"), index=False)

    # --- fig 2: CDBG by category, and the sliver that moves ----------------
    f2 = pd.DataFrame({
        "Category": list(g.index) + ["Actually redistributed"],
        "FY2025 CDBG ($m)": list((g.cdbg / 1e6).round(1)) + [round(moved / 1e6, 1)],
        "Share of program (%)": list((100 * g.cdbg / total_cdbg).round(1))
                                + [round(100 * moved / total_cdbg, 2)],
        "Colour": [COLOURS[c] for c in g.index] + ["#3F7D55"]})
    f2.to_csv(os.path.join(OUT, "fig2_cdbg.csv"), index=False)

    # --- fig 3: the incentive rate -----------------------------------------
    # Denominator is POSITIVE net homes, matching how subsection (b) weights the
    # bonus: it clips the change at zero. Keeps the gains figure equal to the
    # $117.08 per-home rate the script reports, rather than diluting it with the
    # negative change of recipients who qualify but are paid nothing.
    pos = d.assign(p=d.recent_change.clip(lower=0)).groupby("category").p.sum()
    rate = (g.adjustment / pos).round(2)
    f3 = pd.DataFrame({
        "Category": g.index,
        "Dollars per net home added": rate.reindex(g.index).values,
        "Colour": [COLOURS[c] for c in g.index]})
    f3.to_csv(os.path.join(OUT, "fig3_per_home.csv"), index=False)

    # --- fig 4: symbol map --------------------------------------------------
    # places, counties and county subdivisions each need their own centroid file
    cen = pd.read_csv(CENTROIDS, dtype={"geoid": str})[["geoid", "lat", "lon"]]
    cty = pd.read_csv(os.path.join(os.path.dirname(CENTROIDS), "gaz_county_xy.csv"),
                      dtype={"geoid": str})[["geoid", "lat", "lon"]]
    cen = pd.concat([cen, cty], ignore_index=True).drop_duplicates("geoid")
    m = d.merge(cen, on="geoid", how="left")
    # a county subdivision falls back to the centroid of its county
    fb = m.lat.isna() & m.geoid.str.len().eq(10)
    if fb.any():
        cmap = cty.set_index("geoid")
        m.loc[fb, "lat"] = m.loc[fb, "geoid"].str[:5].map(cmap.lat)
        m.loc[fb, "lon"] = m.loc[fb, "geoid"].str[:5].map(cmap.lon)
    m["Outcome"] = m.category
    m["CDBG ($)"] = m.cdbg_fy25.round(0).astype(int)
    m["Adjustment ($)"] = m.adjustment.round(0).astype(int)
    m["Net homes added"] = m.recent_change.round(0)
    cols = ["name", "state", "lat", "lon", "Outcome", "CDBG ($)",
            "Adjustment ($)", "Net homes added"]
    mm = m[cols].rename(columns={"name": "Jurisdiction", "state": "State"})
    missing = mm.lat.isna().sum()
    mm.dropna(subset=["lat", "lon"]).to_csv(os.path.join(OUT, "fig4_map.csv"),
                                            index=False)

    print("wrote figures/ (4 files)")
    print("  net homes added, all recipients: %s" % format(total_net, ",.0f"))
    print("  share in jurisdictions the Act never touches: %.0f%%"
          % (100 * g.loc[["Exempt — recent disaster", "Exempt — other screens"],
                         "net_homes"].sum() / total_net))
    print("  CDBG total $%s; actually redistributed $%s (%.2f%%)"
          % (format(total_cdbg, ",.0f"), format(moved, ",.0f"),
             100 * moved / total_cdbg))
    print("  map rows: %d (%d recipients had no centroid and were dropped)"
          % (len(mm) - missing, missing))
    print()
    print(f1.to_string(index=False))
    print()
    print(f3.to_string(index=False))


if __name__ == "__main__":
    main()
