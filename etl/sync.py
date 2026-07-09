"""
etl/sync.py  ─  Automatic Excel → SQLite sync with incremental geocoding
═════════════════════════════════════════════════════════════════════════
Designed for non-technical users.  The DON only needs to:

    1.  Export updated CustomerData.xlsx and CaregiverData.xlsx into  data/
    2.  Start (or refresh) the Streamlit app

This module will:
    • Read both Excel files
    • Compare every row against the existing DB using a stable surrogate key
    • Geocode ONLY new or address-changed records (via Geocodio)
    • Update all other fields (hours, status, etc.) from the latest Excel
    • Remove DB rows that no longer appear in the Excel files
    • Rebuild the vw_staff_capacity view

Privacy:  Only address strings are ever sent to the Geocodio API.
          Names, phones, and all other PII stay on the local machine.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import warnings
from dataclasses import dataclass, field

import h3
import pandas as pd
import requests
from dotenv import load_dotenv

from src.logger import logger

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
DB_PATH           = "data/staffing_engine_secure.db"
CLIENTS_EXCEL     = "data/CustomerData.xlsx"
STAFF_EXCEL       = "data/CaregiverData.xlsx"
GEOCODIO_ENDPOINT = "https://api.geocod.io/v1.7/geocode"
BATCH_SIZE        = 10_000

ADDRESS_COLS = ["Address 1", "Address 2", "City", "State", "Zip"]

REQUIRED_CLIENT_COLS = ["First Name", "Last Name", "Address 1", "City", "State", "Zip"]
OPTIONAL_CLIENT_COLS = ["Address 2", "Phone", "Gender", "Class", "Birth Date"]

REQUIRED_STAFF_COLS  = ["First Name", "Last Name", "Address 1", "City", "State", "Zip"]
OPTIONAL_STAFF_COLS  = ["Address 2", "Mobile", "Gender", "Status", "Hire Date",
                        "Birth Date", "Skills", "Weekly Max Hours", "Daily Max Hours"]

EXCLUDED_CLIENT_NAMES: list[str] = [
    # Example:  "Test Client",
]

EXCLUDED_STAFF_NAMES: list[str] = [
    "Pragya Chaurasia", "Divy Chaurasia", "Tierra Flowers",
    "Lakisha Rose", "Mohit Aggarwal", "Marquise Lane"
]


# ─── RESULT CONTAINER ─────────────────────────────────────────────────────────

@dataclass
class SyncResult:
    """Returned by sync_db_from_excels(); the app uses this for status messages."""
    ok: bool = True
    error: str = ""

    total_clients: int = 0
    total_staff: int = 0
    new_geocoded: int = 0
    addr_updated: int = 0
    fields_updated: int = 0
    clients_removed: int = 0
    staff_removed: int = 0
    geocode_failures: list = field(default_factory=list)
    missing_optional: dict = field(default_factory=dict)


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _build_address(*parts) -> str:
    return ", ".join(str(p).strip() for p in parts if pd.notna(p) and str(p).strip())


def _addr_for_row(row: pd.Series) -> str:
    parts = []
    for col in ADDRESS_COLS:
        if col in row.index:
            parts.append(row[col])
        else:
            alias = col.replace(" 1", "").replace(" 2", "2")
            if alias in row.index:
                parts.append(row[alias])
    return _build_address(*parts)


def _surrogate_key(row: pd.Series) -> str:
    fname = str(row.get("First Name", "")).strip().lower()
    lname = str(row.get("Last Name", "")).strip().lower()
    addr  = _addr_for_row(row).lower()
    raw_key = f"{fname}|{lname}|{addr}"
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _surrogate_key_name_only(row: pd.Series) -> str:
    fname = str(row.get("First Name", "")).strip().lower()
    lname = str(row.get("Last Name", "")).strip().lower()
    return f"{fname}|{lname}"


def _read_excel(path: str) -> pd.DataFrame:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = pd.read_excel(path, header=0)
    df.columns = df.columns.str.strip()
    return df.dropna(how="all").reset_index(drop=True)


def _check_required_cols(df: pd.DataFrame, required: list, label: str) -> str | None:
    missing = [c for c in required if c not in df.columns]
    if missing:
        return (
            f"The {label} spreadsheet is missing required column(s): "
            f"**{', '.join(missing)}**.\n\n"
            f"Found columns: {', '.join(df.columns)}.\n\n"
            f"Please check the export from the agency system."
        )
    return None


def _fill_optional_cols(df: pd.DataFrame, optional: list, label: str) -> list[str]:
    missing = []
    for col in optional:
        if col not in df.columns:
            df[col] = None
            missing.append(col)
    if missing:
        logger.warning(f"  ⚠ {label}: optional column(s) missing and defaulted to N/A: {missing}")
    return missing


def _select_known_cols(df: pd.DataFrame, required: list, optional: list,
                       extra_keep: list | None = None) -> pd.DataFrame:
    known = set(required + optional + (extra_keep or []))
    keep = [c for c in df.columns if c in known]
    return df[keep]


def _map_role(skill) -> str:
    if pd.isna(skill):
        return "PCA"
    s = str(skill).strip().upper()
    return s if s in ("LPN", "RN") else "PCA"


def _lat_lng_to_h3(lat, lng, resolution: int = 8) -> str | None:
    try:
        if lat is None or lng is None or pd.isna(lat) or pd.isna(lng):
            return None
        return h3.latlng_to_cell(float(lat), float(lng), resolution)
    except (ValueError, TypeError):
        return None


# ─── GEOCODING ────────────────────────────────────────────────────────────────

def _ensure_cache_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS geocode_cache (
            address   TEXT PRIMARY KEY,
            latitude  REAL,
            longitude REAL
        )
    """)
    conn.commit()


def _load_cache(conn: sqlite3.Connection) -> dict:
    _ensure_cache_table(conn)
    rows = conn.execute("SELECT address, latitude, longitude FROM geocode_cache").fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def _save_cache(conn: sqlite3.Connection, entries: dict):
    conn.executemany(
        "INSERT OR REPLACE INTO geocode_cache (address, latitude, longitude) VALUES (?, ?, ?)",
        [(addr, lat, lng) for addr, (lat, lng) in entries.items()],
    )
    conn.commit()


def _batch_geocode(addresses: list, api_key: str) -> dict:
    results = {}
    total = len(addresses)
    for start in range(0, total, BATCH_SIZE):
        chunk = addresses[start: start + BATCH_SIZE]
        logger.info(f"  📡 Geocoding batch {start+1}–{start+len(chunk)} of {total}…")
        try:
            resp = requests.post(
                GEOCODIO_ENDPOINT,
                params={"api_key": api_key},
                json=chunk,
                timeout=120,
            )
        except requests.RequestException as exc:
            logger.error(f"  ✗ Request error: {exc}")
            for addr in chunk:
                results[addr] = (None, None)
            continue

        if resp.status_code != 200:
            logger.error(f"  ✗ HTTP {resp.status_code}: {resp.text[:300]}")
            for addr in chunk:
                results[addr] = (None, None)
            continue

        for addr, item in zip(chunk, resp.json().get("results", [])):
            try:
                candidates = item["response"]["results"]
                if candidates:
                    loc = candidates[0]["location"]
                    results[addr] = (loc["lat"], loc["lng"])
                else:
                    results[addr] = (None, None)
            except (KeyError, IndexError, TypeError):
                results[addr] = (None, None)

    return results


# ─── VIEW REBUILD ─────────────────────────────────────────────────────────────

def _rebuild_view(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schedule (
            Schedule_ID TEXT, Staff_ID TEXT, Hours_Committed REAL
        )
    """)
    conn.execute("DROP VIEW IF EXISTS vw_staff_capacity")
    conn.execute("DROP TABLE IF EXISTS staff_capacity")
    conn.execute("""
        CREATE TABLE staff_capacity AS
        SELECT
            s.Staff_ID,
            s."First Name",
            s."Last Name",
            s.Mobile,
            s.Gender,
            s.Role,
            s.H3_Index,
            s.Max_Weekly_Hours,
            COALESCE(SUM(sch.Hours_Committed), 0) AS Total_Committed_Hours,
            (s.Max_Weekly_Hours - COALESCE(SUM(sch.Hours_Committed), 0)) AS Available_Hours
        FROM staff s
        LEFT JOIN schedule sch ON s.Staff_ID = sch.Staff_ID
        GROUP BY
            s.Staff_ID, s."First Name", s."Last Name", s.Mobile, s.Gender, s.Role,
            s.H3_Index, s.Max_Weekly_Hours
    """)
    conn.commit()


# ─── MAIN SYNC FUNCTION ──────────────────────────────────────────────────────

def sync_db_from_excels() -> SyncResult:
    result = SyncResult()

    for path, label in [(CLIENTS_EXCEL, "Clients"), (STAFF_EXCEL, "Staff")]:
        if not os.path.isfile(path):
            result.ok = False
            result.error = (
                f"**{label} file not found.**\n\n"
                f"Please export `{os.path.basename(path)}` from the agency system "
                f"and place it in the `data/` folder, then restart the app."
            )
            return result

    try:
        clients_xl = _read_excel(CLIENTS_EXCEL)
        staff_xl   = _read_excel(STAFF_EXCEL)
    except Exception as exc:
        result.ok = False
        result.error = f"Error reading Excel files: {exc}"
        return result

    for df, req, label in [
        (clients_xl, REQUIRED_CLIENT_COLS, "Clients"),
        (staff_xl,   REQUIRED_STAFF_COLS,  "Staff"),
    ]:
        err = _check_required_cols(df, req, label)
        if err:
            result.ok = False
            result.error = err
            return result

    missing_client_opt = _fill_optional_cols(clients_xl, OPTIONAL_CLIENT_COLS, "Clients")
    missing_staff_opt  = _fill_optional_cols(staff_xl,   OPTIONAL_STAFF_COLS,  "Staff")
    if missing_client_opt:
        result.missing_optional["Clients"] = missing_client_opt
    if missing_staff_opt:
        result.missing_optional["Staff"] = missing_staff_opt

    if EXCLUDED_CLIENT_NAMES:
        excl_lower = {n.strip().lower() for n in EXCLUDED_CLIENT_NAMES}
        full_names = (clients_xl["First Name"].str.strip() + " " + clients_xl["Last Name"].str.strip()).str.lower()
        before = len(clients_xl)
        clients_xl = clients_xl[~full_names.isin(excl_lower)].reset_index(drop=True)
        dropped = before - len(clients_xl)
        if dropped:
            logger.info(f"  🚫 Excluded {dropped} client(s) by name")

    if EXCLUDED_STAFF_NAMES:
        excl_lower = {n.strip().lower() for n in EXCLUDED_STAFF_NAMES}
        full_names = (staff_xl["First Name"].str.strip() + " " + staff_xl["Last Name"].str.strip()).str.lower()
        before = len(staff_xl)
        staff_xl = staff_xl[~full_names.isin(excl_lower)].reset_index(drop=True)
        dropped = before - len(staff_xl)
        if dropped:
            logger.info(f"  🚫 Excluded {dropped} staff member(s) by name")

    result.total_clients = len(clients_xl)
    result.total_staff   = len(staff_xl)

    load_dotenv(".env")
    api_key = os.getenv("geocodio_api_key") or os.getenv("GEOCODIO_API_KEY")

    conn = sqlite3.connect(DB_PATH)
    cache = _load_cache(conn)

    try:
        db_clients = pd.read_sql_query("SELECT * FROM clients", conn)
    except Exception:
        db_clients = pd.DataFrame()
    try:
        db_staff = pd.read_sql_query("SELECT * FROM staff", conn)
    except Exception:
        db_staff = pd.DataFrame()

    clients_xl["_skey"]      = clients_xl.apply(_surrogate_key, axis=1)
    clients_xl["_name_key"]  = clients_xl.apply(_surrogate_key_name_only, axis=1)
    clients_xl["_addr"]      = clients_xl.apply(_addr_for_row, axis=1)

    staff_xl["_skey"]        = staff_xl.apply(_surrogate_key, axis=1)
    staff_xl["_name_key"]    = staff_xl.apply(_surrogate_key_name_only, axis=1)
    staff_xl["_addr"]        = staff_xl.apply(_addr_for_row, axis=1)

    if not db_clients.empty:
        if "_match_hash" in db_clients.columns:
            db_clients["_skey"] = db_clients["_match_hash"]
        else:
            db_clients["_skey"] = db_clients.apply(_surrogate_key, axis=1)
        db_clients["_name_key"] = db_clients.apply(_surrogate_key_name_only, axis=1)
        existing_client_keys = set(db_clients["_skey"])
        existing_client_name_keys = set(db_clients["_name_key"])
    else:
        existing_client_keys = set()
        existing_client_name_keys = set()

    if not db_staff.empty:
        if "_match_hash" in db_staff.columns:
            db_staff["_skey"] = db_staff["_match_hash"]
        else:
            db_staff["_skey"] = db_staff.apply(_surrogate_key, axis=1)
        db_staff["_name_key"] = db_staff.apply(_surrogate_key_name_only, axis=1)
        existing_staff_keys = set(db_staff["_skey"])
        existing_staff_name_keys = set(db_staff["_name_key"])
    else:
        existing_staff_keys = set()
        existing_staff_name_keys = set()

    addrs_needing_geocoding = set()

    def classify_rows(xl_df, existing_keys, existing_name_keys):
        actions = []
        for _, row in xl_df.iterrows():
            skey = row["_skey"]
            nkey = row["_name_key"]
            addr = row["_addr"]

            if skey in existing_keys:
                actions.append("existing")
            elif nkey in existing_name_keys:
                actions.append("addr_changed")
                addrs_needing_geocoding.add(addr)
            else:
                actions.append("new")
                addrs_needing_geocoding.add(addr)
        xl_df["_action"] = actions

    classify_rows(clients_xl, existing_client_keys, existing_client_name_keys)
    classify_rows(staff_xl, existing_staff_keys, existing_staff_name_keys)

    addrs_to_geocode = [a for a in addrs_needing_geocoding if a not in cache]

    if addrs_to_geocode:
        if api_key:
            logger.info(f"  Geocoding {len(addrs_to_geocode)} new address(es)…")
            new_results = _batch_geocode(addrs_to_geocode, api_key)
            _save_cache(conn, new_results)
            cache.update(new_results)

            failures = [a for a, (lat, _) in new_results.items() if lat is None]
            result.geocode_failures = failures
            result.new_geocoded = len(new_results) - len(failures)
        else:
            logger.warning("  ⚠ GEOCODIO_API_KEY not set — skipping geocoding for new records")
            result.geocode_failures = list(addrs_to_geocode)
    else:
        logger.info("  ✅ All addresses already cached — no Geocodio call needed.")

    def finalise_clients(xl_df: pd.DataFrame) -> pd.DataFrame:
        df = xl_df.copy()
        tmp_lat = [cache.get(a, (None, None))[0] for a in df["_addr"]]
        tmp_lon = [cache.get(a, (None, None))[1] for a in df["_addr"]]
        df["H3_Index"] = [
            _lat_lng_to_h3(lat, lng)
            for lat, lng in zip(tmp_lat, tmp_lon)
        ]
        df = df.rename(columns={"Address 1": "Address", "Address 2": "Address2", "_skey": "_match_hash"})
        df.insert(0, "Client_ID", [f"C{i+1:03d}" for i in range(len(df))])
        df = df.drop(columns=["_name_key", "_addr", "_action"], errors="ignore")
        known = {"Client_ID", "First Name", "Last Name",
                 "Phone", "Class", "_match_hash",
                 "H3_Index"}
        df = df[[c for c in df.columns if c in known]]
        return df

    def finalise_staff(xl_df: pd.DataFrame) -> pd.DataFrame:
        df = xl_df.copy()
        tmp_lat = [cache.get(a, (None, None))[0] for a in df["_addr"]]
        tmp_lon = [cache.get(a, (None, None))[1] for a in df["_addr"]]
        df["H3_Index"] = [
            _lat_lng_to_h3(lat, lng)
            for lat, lng in zip(tmp_lat, tmp_lon)
        ]
        df = df.rename(columns={"Address 1": "Address", "Address 2": "Address2", "_skey": "_match_hash"})
        df.insert(0, "Staff_ID", [f"S{i+1:03d}" for i in range(len(df))])
        df["Role"] = df["Skills"].apply(_map_role) if "Skills" in df.columns else "PCA"
        if "Weekly Max Hours" in df.columns:
            df["Max_Weekly_Hours"] = df["Weekly Max Hours"].apply(
                lambda x: None if pd.isna(x) or str(x).strip().lower() == "no max" else int(x)
            )
        else:
            df["Max_Weekly_Hours"] = None
        df = df.drop(columns=["_name_key", "_addr", "_action"], errors="ignore")
        known = {"Staff_ID", "First Name", "Last Name",
                 "Mobile", "Gender", "Status",
                 "Skills", "Weekly Max Hours",
                 "Daily Max Hours", "Role", "Max_Weekly_Hours", "_match_hash",
                 "H3_Index"}
        df = df[[c for c in df.columns if c in known]]
        return df

    clients_final = finalise_clients(clients_xl)
    staff_final   = finalise_staff(staff_xl)

    result.addr_updated    = int((clients_xl["_action"] == "addr_changed").sum() +
                                 (staff_xl["_action"]   == "addr_changed").sum())
    result.fields_updated  = int((clients_xl["_action"] == "existing").sum() +
                                  (staff_xl["_action"]   == "existing").sum())

    xl_client_name_keys = set(clients_xl["_name_key"])
    xl_staff_name_keys  = set(staff_xl["_name_key"])
    if not db_clients.empty:
        result.clients_removed = int((~db_clients["_name_key"].isin(xl_client_name_keys)).sum())
    if not db_staff.empty:
        result.staff_removed = int((~db_staff["_name_key"].isin(xl_staff_name_keys)).sum())

    logger.info(f"  Writing {len(clients_final)} clients, {len(staff_final)} staff to DB…")
    clients_final.to_sql("clients", conn, if_exists="replace", index=False)
    staff_final.to_sql("staff",     conn, if_exists="replace", index=False)
    conn.commit()

    _rebuild_view(conn)
    conn.close()

    logger.info("  ✅ Sync complete.")
    return result
