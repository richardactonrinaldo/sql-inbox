"""Build data/lakeshore_ed.db, the fictional SQLite database used by the app.

Run once (python make_database.py). It splits the same 300 emergency department visits used
in the Excel version (data/ED_Visits_2025.csv) into four linked tables:

    sites           one row per ED site (plus one site that hasn't opened yet: no visits)
    triage_levels   the five CTAS levels with their target wait times
    patients        one row per patient (some have several visits; a few have none yet)
    visits          one row per ED visit

The seed is fixed, so every run builds the same database.
"""
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
DB = HERE / "data" / "lakeshore_ed.db"
rng = np.random.default_rng(2025)

src = pd.read_csv(HERE / "data" / "ED_Visits_2025.csv")

SITES = [  # site_id, site_name, city, ed_beds, status
    (1, "Barrie", "Barrie", 38, "Open"),
    (2, "Orillia", "Orillia", 24, "Open"),
    (3, "Midland", "Midland", 16, "Open"),
    (4, "Collingwood", "Collingwood", 18, "Open"),
    (5, "Bracebridge", "Bracebridge", 12, "Opening 2026"),  # no visits yet: useful for LEFT JOIN
]
SITE_ID = {name: sid for sid, name, *_ in SITES}

TRIAGE = [  # Canadian Triage and Acuity Scale (CTAS) levels and target time to physician
    (1, "Resuscitation", 0),
    (2, "Emergent", 15),
    (3, "Urgent", 30),
    (4, "Less urgent", 60),
    (5, "Non-urgent", 120),
]

HOME_TOWNS = {
    "Barrie": (["Barrie", "Innisfil", "Oro-Medonte", "Springwater"], [60, 20, 10, 10]),
    "Orillia": (["Orillia", "Severn", "Ramara", "Oro-Medonte"], [60, 20, 12, 8]),
    "Midland": (["Midland", "Penetanguishene", "Tay", "Tiny"], [50, 25, 15, 10]),
    "Collingwood": (["Collingwood", "Wasaga Beach", "Clearview", "The Blue Mountains"], [50, 25, 15, 10]),
}


def pick(options, weights):
    w = np.array(weights, dtype=float)
    return options[rng.choice(len(options), p=w / w.sum())]


# ---- patients: some people come back. Visits by the same sex and age are sometimes the same person.
src["birth_year"] = 2025 - src["Age"]
owner = {}  # visit index -> patient key
next_key = 0
for (_, _), grp in src.groupby(["Sex", "Age"], sort=False):
    idx = list(grp.index)
    rng.shuffle(idx)
    i = 0
    while i < len(idx):
        size = 1
        if i + 1 < len(idx) and rng.random() < 0.45:   # a returning patient
            size = 3 if (i + 2 < len(idx) and rng.random() < 0.2) else 2
        for j in idx[i:i + size]:
            owner[j] = next_key
        next_key += 1
        i += size
src["pkey"] = src.index.map(owner)

# Number patients in order of their first visit (P0001 = first patient seen in 2025).
first_visit = src.groupby("pkey")["VisitDate"].min().sort_values(kind="stable")
pid = {k: f"P{n:04d}" for n, k in enumerate(first_visit.index, 1)}
src["patient_id"] = src["pkey"].map(pid)

patients = []
for k in first_visit.index:
    v = src[src["pkey"] == k].iloc[0]
    towns, w = HOME_TOWNS[v["Site"]]
    patients.append((pid[k], v["Sex"], int(v["birth_year"]), pick(towns, w)))
# A few registered patients with no 2025 ED visits (e.g. pre-registered at a clinic): for LEFT JOIN.
n = len(patients)
for extra in range(1, 13):
    site = pick(list(HOME_TOWNS), [40, 25, 18, 17])
    towns, w = HOME_TOWNS[site]
    patients.append((f"P{n + extra:04d}", "F" if rng.random() < 0.5 else "M",
                     int(rng.integers(1940, 2020)), pick(towns, w)))

# ---- visits. A few costs are still NULL (billing not finalized): for IS NULL and COUNT(column).
cost = src["CostCAD"].astype("Int64").astype(object)
pending = rng.choice(src.index[src["Disposition"].isin(["Admitted", "Transferred"])], size=7, replace=False)
cost.loc[pending] = None
visits = [
    (r.VisitID, r.patient_id, SITE_ID[r.Site], r.VisitDate, int(r.TriageLevel), r.ChiefComplaint,
     int(r.WaitTimeMin), float(r.LengthOfStayHrs), r.Disposition, None if c is None else int(c))
    for r, c in zip(src.itertuples(index=False), cost)
]

# ---- write the database
DB.unlink(missing_ok=True)
con = sqlite3.connect(DB)
con.executescript("""
CREATE TABLE sites (
    site_id      INTEGER PRIMARY KEY,
    site_name    TEXT NOT NULL,
    city         TEXT NOT NULL,
    ed_beds      INTEGER NOT NULL,
    status       TEXT NOT NULL
);
CREATE TABLE triage_levels (
    triage_level     INTEGER PRIMARY KEY,
    description      TEXT NOT NULL,
    target_wait_min  INTEGER NOT NULL
);
CREATE TABLE patients (
    patient_id   TEXT PRIMARY KEY,
    sex          TEXT NOT NULL,
    birth_year   INTEGER NOT NULL,
    home_town    TEXT NOT NULL
);
CREATE TABLE visits (
    visit_id            TEXT PRIMARY KEY,
    patient_id          TEXT NOT NULL REFERENCES patients(patient_id),
    site_id             INTEGER NOT NULL REFERENCES sites(site_id),
    visit_date          TEXT NOT NULL,
    triage_level        INTEGER NOT NULL REFERENCES triage_levels(triage_level),
    chief_complaint     TEXT NOT NULL,
    wait_time_min       INTEGER NOT NULL,
    length_of_stay_hrs  REAL NOT NULL,
    disposition         TEXT NOT NULL,
    cost_cad            INTEGER
);
""")
con.executemany("INSERT INTO sites VALUES (?,?,?,?,?)", SITES)
con.executemany("INSERT INTO triage_levels VALUES (?,?,?)", TRIAGE)
con.executemany("INSERT INTO patients VALUES (?,?,?,?)", patients)
con.executemany("INSERT INTO visits VALUES (?,?,?,?,?,?,?,?,?,?)", visits)
con.commit()

for t in ["sites", "triage_levels", "patients", "visits"]:
    print(t, con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0], "rows")
print("patients with 2+ visits:", con.execute(
    "SELECT COUNT(*) FROM (SELECT patient_id FROM visits GROUP BY patient_id HAVING COUNT(*) > 1)").fetchone()[0])
print("NULL costs:", con.execute("SELECT COUNT(*) FROM visits WHERE cost_cad IS NULL").fetchone()[0])
con.execute("VACUUM")
con.close()
