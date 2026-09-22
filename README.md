# Build Now Act (SEC. 213) — replication

One script reproduces the figures in the article. It downloads the public
sources, applies the four eligibility screens in subsection (a)(3), computes the
housing growth improvement rate, and runs the reallocation in subsection (b).

```
pip install pandas
python build_now.py --census-key YOUR_CENSUS_API_KEY
python build_now.py --census-key YOUR_KEY --extras          # the two side analyses
python build_now.py --census-key YOUR_KEY --series bps      # alternative unit series
python build_now.py --census-key YOUR_KEY --zoning-rule counties
```

A Census API key is free: https://api.census.gov/data/key_signup.html
Everything downloaded is cached under `cache/`; delete it to refetch. The first
run takes a few minutes, later runs are offline and identical.

## Inputs (`inputs/`)

| File | What it is |
|---|---|
| `jurisdictions.csv` | The 1,209 CDBG entitlement recipients and their FY2025 awards |
| `jurisdiction_counties.csv` | Jurisdiction → county, **many-to-many**: 117 recipients span more than one county, up to five. This is what makes the disaster screen contentious |
| `urban_county_carveouts.csv` | Municipalities separately funded inside an urban county, with the share of each lying in the county. An urban county's award covers only the rest |
| `safmr_by_jurisdiction.csv` | Median Small Area Fair Market Rent per recipient. Shipped rather than downloaded because HUD blocks scripted pulls of the SAFMR workbook |

Connecticut needs care and the crosswalk already handles it: the state replaced
its counties with nine planning regions in 2022, Census geography followed, and
FEMA did not. Recipients carrying a planning-region code can never match a FEMA
designation, so the crosswalk maps them back to the legacy counties FEMA uses.

## Downloads

* **ACS 5-year** — housing units (B25001), median home value (B25077), renter-occupied and vacant-for-rent (B25003, B25004)
* **Building Permits Survey** — annual units authorised, place, county and MCD files
* **OpenFEMA** — DR and EM declarations by designated county

## Why not the Address Count Listing Files, which the statute names?

Because for these years they cannot produce the statute's own metric. Census
publishes ACL vintages for 2020, 2023, 2024, 2025 and 2026 — nothing before
2020, nothing for 2021 or 2022. The prior-growth window for an FY2025 allocation
runs 2014–2019, and for FY2029 it runs FY2018–FY2023; the ACL reaches neither.
Not until FY2031 does a prior window fall entirely inside the published range.
Any ACL-based growth rate for today is a splice onto another series.

So the script offers the two series that can be built end to end, and reports
how far apart they are. That gap is a finding, not a nuisance: the aggregate
barely moves between them, and **42 per cent of scored jurisdictions change
sides**.

| | ACS | BPS |
|---|---:|---:|
| Median HGIR | +0.2927 | +0.0347 |
| Penalised / bonus | 85 / 97 | 75 / 82 |
| Redistributed | $11,597,681 | $10,957,110 |
| Per net home added | $117.08 | $94.36 |
| Recipients scored | 182 | 157 |

## Choices the statute leaves open

* **Screen (D), zoning.** No reading of "lacks the legal authority to enact or update zoning and permitting ordinances" reproduces any published coding of it, so the baseline applies no zoning screen. `--zoning-rule counties` excludes every county-type recipient instead, which takes the eligible set from 182 to 130.
* **Screen (B), the national vacancy rate.** The default 6.8 per cent is the CPS/HVS *annual* rate for 2024. The ACS 5-year 2024 figure computed on the same basis as the local rates is 5.5507 per cent, and the choice moves about 170 jurisdictions. Use `--vacancy-rate`.
* **Screen (C), geography.** Any county the recipient touches counts, which follows 44 C.F.R. § 206.40(b) — a designated area "includes all local government jurisdictions within its boundaries."

## `--extras`

**Rolling exemption rate.** The share of recipients the disaster screen would
exempt, for 3-year windows anchored on 1 May of each year. Windows containing
the March 2020 COVID-19 declarations are flagged and reported separately,
because those declarations covered effectively every county in the country and
exempt everyone. County-level COVID designations run 13 March to 17 April 2020, so three
anchors are affected, leaving **seven clean windows, 2017-2019 and 2023-2026**,
averaging 61.4 per cent.

**Permits before and after a declaration.** Building permits per 1,000 housing
units in the three years after a qualifying declaration against the three
before, de-meaned by year so the national construction cycle is removed. COVID
is excluded; with it in there is no comparison group. Descriptive only —
disasters are not randomly assigned.

## Known limitations

* **Demolitions.** The BPS counts authorisations and nets no losses at all. B25001 does net them, but indirectly: ACS housing-unit totals are controlled to the Census Population Estimates, which subtract losses from modelled rates rather than local counts. No federal source measures housing losses at local level.
* **The two unit measures disagree sharply, and this drives the bonus weights.** Salt Lake City's ACS net change of 3,870 units compares with 1,283 permits; Gilbert's 5,353 with 1,858. Across bonus recipients with both measures the median ratio is 1.21, with quartiles at 0.71 and 1.68.
* BPS scores 25 fewer recipients than the ACS, because not every entitlement jurisdiction reports to the permits survey.
* Screen (A) depends on the shipped SAFMR file; 21 recipients have no SAFMR and are treated as not exempt under (A).
* **The permit comparison has an arbitrary control event year.** Jurisdictions with no qualifying declaration are scored against a fixed 2019 anchor, and a jurisdiction declared in 2016 or 2017 falls into that control group. Urban counties are excluded from the comparison because their permits cover the whole county while the unit panel counts only the funded area.
* **The rolling exemption rate rests on seven windows**, not ten: the 2020, 2021 and 2022 anchors contain the county-level March–April 2020 COVID-19 designations, leaving 2017–2019 and 2023–2026. Statewide COVID rows carrying no county code are excluded throughout, as they designate no area.

## Licence and reuse

The code in this repository is MIT-licensed — see `LICENSE`. Use it, change it,
publish what you find.

The data is a different matter and mostly not mine to license. The underlying
sources are works of the United States government and are not subject to
copyright under 17 U.S.C. § 105:

* housing units, home values and vacancy rates — U.S. Census Bureau, ACS 5-year
* units authorised — U.S. Census Bureau, Building Permits Survey
* disaster declarations — FEMA, OpenFEMA
* CDBG entitlement awards and Small Area Fair Market Rents — U.S. Department of Housing and Urban Development

What is mine is the compilation: the recipient universe, the many-to-many
jurisdiction-to-county crosswalk, the urban-county carve-out shares, and the
derived outputs in `outputs/` and `figures/`. Those are offered under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) — a link back to the article is plenty.

The results file is committed so the numbers can be checked
without a Census API key.
