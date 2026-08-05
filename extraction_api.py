"""
Sandman unified extraction API.

One upload endpoint behind the Sandman "upload production plan" UI:
  - IMAGE upload (.png/.jpg/.jpeg/.webp/.bmp)  -> Gemini vision reads the
    handwritten board (board_extractor.py, same folder).
  - SPREADSHEET upload (.xlsx/.xls/.csv)       -> work-order parser adapted
    from the plan-processing script (multi-level headers, GPI code / fuzzy
    name resolution).

Both paths return the SAME response shape, which drives the
"Confirm before adding" review table:

POST /extract  (multipart, field "file")
{
  "source_file": "plan.xlsx",
  "file_type": "image" | "spreadsheet",
  "row_count": 10,
  "rows": [
    {
      "order_no": "1",
      "source_text": "ESC 3FT",          # as written / as in the file
      "boxes": "61",                      # plan qty; "??" if unreadable
      "matches": [                        # ranked, best first; [] = no match
        {"component_id": "51011280010",
         "name": "ESCORTS CH 3FT 51011280010",
         "score": 77.0},
        ...
      ]
    }, ...
  ]
}
GET /health -> {"status":"ok", "master_rows":N, "gemini_model":"..."}

Deps: pip install fastapi uvicorn python-multipart google-generativeai \
                  pillow pandas rapidfuzz openpyxl
Run:  python sandman_extraction_api.py            (port 8077)

board_extractor.py and the component master Excel must sit in the
same folder.
"""

import os
import re
import tempfile
from urllib.parse import quote_plus
import pandas as pd
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from rapidfuzz import process
from pydantic import BaseModel
from typing import List, Optional
from datetime import date as _date, timedelta as _timedelta

# Reuses the Gemini extraction + normalization + scorer + config
import board_extractor as gbe

app = FastAPI(title="Sandman Unified Extraction API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten to the Sandman host in production
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
SHEET_EXTS = {".xlsx", ".xls", ".csv"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# ===========================================================
# MASTER (loaded once) -- needs both Component ID and Name so the UI can
# show/change the ID behind each match
# ===========================================================

_master = None            # DataFrame with Component ID / Component Name / Clean
_clean_master = None      # normalized names for fuzzy matching
_valid_ids = None         # set of component IDs for GPI-code prefix matching
_master_source = "empty"  # "excel" | "db_push" | "empty"

WEIGHT_COLUMNS = ["Bunch Weight (Kg)", "Core Weight (Kg)", "Sand Weight (Kg)"]

def _build_master(df, source):
    """Rebuild all matching structures from a DataFrame with canonical
    columns: Component ID, Component Name, and the three weight columns.
    Called both for the Excel bootstrap and for DB pushes from Sandman."""
    global _master, _clean_master, _valid_ids, _master_source
    df = df.copy()
    df["Component ID"] = df["Component ID"].astype(str)
    df["Component Name"] = df["Component Name"].astype(str)
    for wc in WEIGHT_COLUMNS:
        if wc not in df.columns:
            df[wc] = 0.0
        df[wc] = pd.to_numeric(df[wc], errors="coerce").fillna(0.0)
    df["Clean"] = df["Component Name"].apply(gbe.normalize_for_match)
    _master = df
    _clean_master = df["Clean"].tolist()
    _valid_ids = set(df["Component ID"])
    _master_source = source

def _load_master_from_excel():
    """Bootstrap from the Excel in config.py if it exists. In production the
    master comes from Sandman's DB via POST /master, so a missing Excel is
    not fatal -- the API starts empty and waits for the push."""
    if not os.path.exists(gbe.MASTER_FILE):
        print(f"NOTE: master Excel '{gbe.MASTER_FILE}' not found -- starting "
              f"with an empty master. Push master data via POST /master.")
        return
    m = pd.read_excel(gbe.MASTER_FILE, skiprows=5).fillna("")
    for col in (gbe.COMPONENT_COLUMN, "Component ID"):
        if col not in m.columns:
            raise RuntimeError(f"'{col}' not found in master file. "
                               f"Available: {list(m.columns)}")
    m = m.rename(columns={gbe.COMPONENT_COLUMN: "Component Name"})
    _build_master(m, "excel")

def _load_master_from_db():
    """Query the component master directly from Sandman's MySQL database..."""
    from config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD, DB_TABLE, DB_COLUMN_MAP
    if not DB_HOST:
        return False
    try:
        from sqlalchemy import create_engine, text
    except ImportError:
        print("NOTE: DB_HOST is set but sqlalchemy/pymysql aren't installed...")
        return False
    try:
        url = f"mysql+pymysql://{DB_USER}:{quote_plus(DB_PASSWORD)}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
        engine = create_engine(url, pool_pre_ping=True)
        cols_sql = ", ".join(f"`{db_col}` AS `{canon}`" for canon, db_col in DB_COLUMN_MAP.items())
        query = text(f"SELECT {cols_sql} FROM `{DB_TABLE}`")
        with engine.connect() as conn:
            df = pd.read_sql(query, conn)
        df = df.rename(columns={
          "component_id": "Component ID",
          "component_name": "Component Name",
          "bunch_weight": "Bunch Weight (Kg)",
          "core_weight": "Core Weight (Kg)",
          "sand_weight": "Sand Weight (Kg)",
      })
        if df.empty:
            print(f"NOTE: DB query returned 0 rows...")
            return False
        _build_master(df, "db_pull")
        print(f"Loaded {len(df)} components from MySQL...")
        return True
    except Exception as e:
        print(f"NOTE: could not load master from MySQL ({e}) -- falling back.")
        return False

if not _load_master_from_db():
    _load_master_from_excel()

def _require_master():
    if _master is None or len(_master) == 0:
        raise HTTPException(503, "Component master not loaded. Push master "
                                 "data via POST /master first.")

# ===========================================================
# SHARED MATCHING -> ranked [{component_id, name, score}]
# ===========================================================

def rank_matches(query_text):
    q = gbe.normalize_query(query_text)
    if not q:
        return []
    found = process.extract(q, _clean_master, scorer=gbe.match_score,
                            limit=gbe.MATCH_LIMIT)
    out = []
    for _, score, idx in found:
        if score < gbe.MATCH_THRESHOLD:
            continue
        out.append({
            "component_id": _master.iloc[idx]["Component ID"],
            "name": _master.iloc[idx]["Component Name"],
            "score": round(score, 1),
        })
    return out

# ===========================================================
# IMAGE PATH (Gemini)
# ===========================================================

def extract_from_image(tmp_path, filename):
    records = gbe.extract_rows(tmp_path)   # Gemini vision + safety nets
    rows = []
    for rec in records:
        rows.append({
            "order_no": rec["Sr.No"],
            "source_text": rec["Item Name"],
            "boxes": rec["Plan"],
            "matches": rank_matches(rec["Item Name"]),
        })
    return rows

# ===========================================================
# SPREADSHEET PATH (work-order parser, adapted from the plan script)
# ===========================================================

DATE_HEADER_PATTERN = re.compile(r"^(\d{1,2}(?:/\d{1,2}){1,2})\.(\d{2})\.(\d{4})$")

def _clean_col(col_tuple):
    parts = []
    for c in col_tuple:
        if pd.notna(c):
            c = str(c).strip()
            if ("Unnamed" in c) or DATE_HEADER_PATTERN.match(c):
                continue
            parts.append(c)
    seen = set()
    parts = [x for x in parts if not (x in seen or seen.add(x))]
    return "_".join(parts)

def _resolve_by_gpi_code(code):
    """Longest digit-prefix of the GPI code that is a known Component ID."""
    if pd.isna(code):
        return None
    code_part = str(code).split("+")[0]
    digits = re.sub(r"\D", "", code_part)
    for i in range(len(digits), 0, -1):
        if digits[:i] in _valid_ids:
            return digits[:i]
    return None

def _find_columns(df):
    """Locate the ITEMS / GPI code / order / boxes columns by header names,
    so minor header variations don't break the parse."""
    cols = {str(c).upper(): c for c in df.columns}
    def pick(*needles, exclude=()):
        # exact match wins first (avoids 'BOX' hitting 'GCW /BOX_KG')
        for n in needles:
            if n in cols:
                return cols[n]
        for up, orig in cols.items():
            if any(n in up for n in needles) and not any(x in up for x in exclude):
                return orig
        return None
    return {
        "items": pick("ITEMS", "ITEM NAME", "COMPONENT NAME"),
        "gpi":   pick("GPI"),
        "order": pick("#", "ORDER", "SR"),
        "boxes": pick("BOX", exclude=("GCW", "KG", "WEIGHT")),
    }

def _extract_header_dates(raw_columns):
    """Scan the raw (pre-clean) multi-level header tuples for
    'DD/DD(/DD).MM.YYYY' and return ALL days mentioned as a list of date
    objects, in the order listed (e.g. "14/15.07.2026" -> [14th, 15th];
    "14/15/16.07.2026" -> [14th, 15th, 16th]). None if no such header."""
    from datetime import date
    for col_tuple in raw_columns:
        cells = col_tuple if isinstance(col_tuple, tuple) else (col_tuple,)
        for c in cells:
            if pd.notna(c):
                m = DATE_HEADER_PATTERN.match(str(c).strip())
                if m:
                    days_part, month_str, year_str = m.groups()
                    year, month = int(year_str), int(month_str)
                    return [date(year, month, int(d)) for d in days_part.split("/")]
    return None

def _extract_start_date(raw_columns):
    """Convenience wrapper: first header date as an ISO string, or None."""
    dates = _extract_header_dates(raw_columns)
    return dates[0].isoformat() if dates else None

def extract_from_spreadsheet(tmp_path, filename):
    ext = os.path.splitext(filename)[1].lower()
    df = None
    header_dates = None
    if ext == ".csv":
        df = pd.read_csv(tmp_path)
    else:
        # Work-order layout: 2 junk rows then a 3-level header
        # (e.g. '5 STAR WORK ORDER ...'). Fall back to a flat read if the
        # multi-header parse doesn't yield the expected columns.
        try:
            raw = pd.read_excel(tmp_path, skiprows=2, header=[0, 1, 2])
            header_dates = _extract_header_dates(raw.columns)
            raw.columns = [_clean_col(c) for c in raw.columns]
            raw = raw.dropna(axis=1, how="all")
            if _find_columns(raw)["items"]:
                df = raw
        except Exception:
            df = None
        if df is None:
            df = pd.read_excel(tmp_path)

    colmap = _find_columns(df)
    if not colmap["items"]:
        raise HTTPException(422,
            "Could not find an ITEMS / component-name column in this file. "
            f"Columns seen: {list(df.columns.astype(str))[:15]}")

    items_c, gpi_c, order_c, boxes_c = (colmap["items"], colmap["gpi"],
                                        colmap["order"], colmap["boxes"])
    df = df.dropna(subset=[items_c])
    if gpi_c:
        df = df[~(df[items_c].astype(str).str.strip().eq("") &
                  df[gpi_c].isna())]
    # Trailing note/comment rows (e.g. "LAST UPDATE-...", "COPE MOLD FALL
    # PROBLEM") have real text in ITEMS but neither a GPI code nor a BOX
    # value -- that combination means it isn't an actual plan line, so
    # drop it here rather than letting it surface as a spurious
    # "BOX not numeric" skipped row later.
    if gpi_c and boxes_c:
        df = df[~(df[gpi_c].isna() & df[boxes_c].isna())]

    rows = []
    seq = 0
    for _, r in df.iterrows():
        item = str(r[items_c]).strip()
        if not item or item.lower() == "nan":
            continue
        seq += 1

        # 1) exact resolution via GPI code digit-prefix (score 100)
        matches = []
        if gpi_c is not None:
            cid = _resolve_by_gpi_code(r[gpi_c])
            if cid is not None:
                name = _master.loc[_master["Component ID"] == cid,
                                   "Component Name"]
                matches.append({
                    "component_id": cid,
                    "name": name.iloc[0] if len(name) else "",
                    "score": 100.0,
                })
        # 2) fuzzy on the item name (appended as alternatives, or as the
        #    primary matches when the code didn't resolve)
        for fm in rank_matches(item):
            if not any(m["component_id"] == fm["component_id"] for m in matches):
                matches.append(fm)

        order_val = r[order_c] if order_c is not None and pd.notna(r.get(order_c)) else seq
        boxes_val = r[boxes_c] if boxes_c is not None and pd.notna(r.get(boxes_c)) else "??"
        # integers arrive as 1.0 / 61.0 from Excel -- present them cleanly
        def as_str(v):
            s = str(v).strip()
            return s[:-2] if s.endswith(".0") else s

        rows.append({
            "order_no": as_str(order_val),
            "source_text": item,
            "boxes": as_str(boxes_val),
            "matches": matches[:gbe.MATCH_LIMIT],
        })
    return rows, (header_dates[0].isoformat() if header_dates else None), header_dates

# ===========================================================
# ENDPOINTS
# ===========================================================

@app.get("/health")
def health():
    return {"status": "ok",
            "master_rows": 0 if _master is None else len(_master),
            "master_source": _master_source,
            "gemini_model": gbe.GEMINI_MODEL}


class MasterRow(BaseModel):
    component_id: str
    component_name: str
    bunch_weight: float = 0.0
    core_weight: float = 0.0
    sand_weight: float = 0.0

class MasterPush(BaseModel):
    rows: List[MasterRow]

@app.post("/master")
def push_master(payload: MasterPush):
    """Sandman pushes the component master from its DB. Replaces the
    in-memory master entirely; call again whenever components change.
    All matching (GPI codes, fuzzy names, weights) immediately uses the
    new data -- no restart needed."""
    if not payload.rows:
        raise HTTPException(422, "rows is empty")
    df = pd.DataFrame([{
        "Component ID": r.component_id,
        "Component Name": r.component_name,
        "Bunch Weight (Kg)": r.bunch_weight,
        "Core Weight (Kg)": r.core_weight,
        "Sand Weight (Kg)": r.sand_weight,
    } for r in payload.rows])
    _build_master(df, "db_push")
    return {"status": "ok", "master_rows": len(df), "master_source": "db_push"}
  
@app.post("/master/refresh-from-db")
def refresh_master_from_db():
    """Re-query the master directly from MySQL... without restarting the service."""
    ok = _load_master_from_db()
    if not ok:
        raise HTTPException(503, "Could not load master from the database...")
    return {"status": "ok", "master_rows": len(_master), "master_source": _master_source}

@app.post("/extract")
async def extract(file: UploadFile = File(...)):
    _require_master()
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext in IMAGE_EXTS:
        file_type = "image"
    elif ext in SHEET_EXTS:
        file_type = "spreadsheet"
    elif ext == ".pdf":
        raise HTTPException(400, "PDF upload is not supported yet -- upload "
                                 "the board photo as JPEG/PNG, or the plan "
                                 "as XLSX/CSV.")
    else:
        raise HTTPException(400, f"Unsupported file type '{ext}'.")

    contents = await file.read()
    if not contents:
        raise HTTPException(400, "Uploaded file is empty.")
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "File too large (limit 25 MB).")

    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    try:
        tmp.write(contents)
        tmp.close()
        try:
            if file_type == "image":
                rows = extract_from_image(tmp.name, file.filename)
            else:
                rows, plan_start_date, header_dates = extract_from_spreadsheet(tmp.name, file.filename)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"Extraction failed: {e}")
    finally:
        os.unlink(tmp.name)

    if file_type == "image":
        # Vision extraction is uncertain -> full confirm-table shape
        return {
            "source_file": file.filename,
            "file_type": "image",
            "plan_start_date": None,   # UI's Date field supplies it
            "row_count": len(rows),
            "rows": rows,
        }

    # Spreadsheet: GPI codes resolve components deterministically, so the
    # full main.py pipeline runs in one shot -- ID resolution, 500-box
    # shift binning, shift/date sequence -- returning the final rows:
    # Order, Date, Shift, BOX, Component ID.
    if not plan_start_date:
        raise HTTPException(422,
            "The plan file has no date header (e.g. '14/15.07.2026') in its "
            "column row, so shifts/dates cannot be assigned.")

    start = _date.fromisoformat(plan_start_date)

    base_rows = []
    skipped = []
    for r in rows:
        # Component ID: GPI-code prefix match leads at score 100, else best
        # fuzzy name match at >= 70 (main.py behavior), else null
        component_id = None
        if r["matches"] and r["matches"][0]["score"] >= 70:
            component_id = r["matches"][0]["component_id"]
        try:
            box = float(r["boxes"])
        except (ValueError, TypeError):
            skipped.append({"Order": r["order_no"], "reason": f"BOX not numeric: {r['boxes']!r}"})
            continue
        base_rows.append({"Order": r["order_no"], "Component ID": component_id,
                          "BOX": box})

    if not base_rows:
        raise HTTPException(422, "No usable rows (all lacked a numeric BOX).")

    binned = _split_into_bins(base_rows, BIN_SIZE)
    num_bins = max(r["Bin"] for r in binned)
    # 2 header dates -> standard full-cycle-per-day; 3 header dates ->
    # one-shift-per-day-then-pile-onto-last-date (see _build_sequence_for)
    seq = _build_sequence_for(header_dates, start, num_bins)

    simple_rows = []
    bin_counters = {}
    for r in binned:
        label = seq[r["Bin"]]
        bin_counters[r["Bin"]] = bin_counters.get(r["Bin"], 0) + 1
        simple_rows.append({
            "Order": bin_counters[r["Bin"]],   # per-shift numbering
            "Date": label["Date"],
            "Shift": label["Shift"],
            "BOX": r["BOX"],
            "Component ID": r["Component ID"],  # null = needs manual attention
        })

    return {
        "file_type": "spreadsheet",
        "bin_size": BIN_SIZE,
        "num_shift_bins": num_bins,
        "skipped_rows": skipped,
        "row_count": len(simple_rows),
        "rows": simple_rows,
    }

# ===========================================================
# /process-plan -- runs AFTER the user confirms the rows in the UI.
#
# This is main.py's downstream pipeline, verbatim logic:
#   * split_into_bins: rows split across shift-bins of BIN_SIZE boxes,
#     splitting a row when it doesn't fit the remaining bin space
#   * build_shift_date_sequence: bins map to real Shift/Date labels --
#     the plan is handed over before 3rd shift, so bin 1 is always
#     Shift 3 on the start date, then 1 -> 2 -> 3 per following day
#   * weights: BOX x Bunch/Core/Sand weight from the component master
# ===========================================================



from config import BIN_SIZE   # boxes per shift-bin (config.py is required)

class ConfirmedRow(BaseModel):
    order_no: str
    component_id: str
    boxes: float

class ProcessPlanRequest(BaseModel):
    start_date: str                 # ISO "YYYY-MM-DD" -- from /extract's
                                    # plan_start_date or the UI's Date field
    rows: List[ConfirmedRow]
    bin_size: Optional[int] = None  # override BIN_SIZE if ever needed

def _split_into_bins(rows, bin_size):
    """main.py's split_into_bins on confirmed rows."""
    result = []
    current_bin = 1
    current_sum = 0
    for row in rows:
        val = row["BOX"]
        while val > 0:
            remaining_space = bin_size - current_sum
            if val <= remaining_space:
                new_row = dict(row)
                new_row["BOX"] = val
                new_row["Bin"] = current_bin
                result.append(new_row)
                current_sum += val
                val = 0
            else:
                new_row = dict(row)
                new_row["BOX"] = remaining_space
                new_row["Bin"] = current_bin
                result.append(new_row)
                val -= remaining_space
                current_bin += 1
                current_sum = 0
        if current_sum == bin_size:
            current_bin += 1
            current_sum = 0
    return result

def _build_shift_date_sequence(start_date, num_bins):
    """main.py's build_shift_date_sequence: first bin is Shift 3 on the
    start date, then 1/2/3 for each following day. Used when the plan
    header lists exactly 2 dates."""
    rows = []
    current_date = start_date
    shift_value = 1
    rows.append({"Shift": "3", "Date": current_date.strftime("%d-%m-%Y"),
                 "Shift_Value": shift_value})
    shift_value += 1
    current_date += _timedelta(days=1)
    while len(rows) < num_bins:
        for s in ["1", "2", "3"]:
            if len(rows) >= num_bins:
                break
            rows.append({"Shift": s, "Date": current_date.strftime("%d-%m-%Y"),
                         "Shift_Value": shift_value})
            shift_value += 1
        current_date += _timedelta(days=1)
    return {r["Shift_Value"]: r for r in rows}

def _build_shift_date_sequence_3day(header_dates, num_bins):
    """Used when the plan header lists exactly 3 dates (e.g.
    '14/15/16.07.2026'): the first date is always Shift 3; each subsequent
    header date gets exactly one shift (Shift 1) in a first pass; any
    further bins pile onto the LAST header date (Shift 2, then Shift 3);
    any bins beyond that continue past the last header date using the
    normal full-cycle-per-day logic."""
    rows = []
    shift_value = 1
    rows.append({"Shift": "3", "Date": header_dates[0].strftime("%d-%m-%Y"),
                 "Shift_Value": shift_value})
    shift_value += 1
    for d in header_dates[1:]:
        if len(rows) >= num_bins:
            break
        rows.append({"Shift": "1", "Date": d.strftime("%d-%m-%Y"),
                     "Shift_Value": shift_value})
        shift_value += 1
    last_date = header_dates[-1]
    for s in ["2", "3"]:
        if len(rows) >= num_bins:
            break
        rows.append({"Shift": s, "Date": last_date.strftime("%d-%m-%Y"),
                     "Shift_Value": shift_value})
        shift_value += 1
    current_date = last_date + _timedelta(days=1)
    while len(rows) < num_bins:
        for s in ["1", "2", "3"]:
            if len(rows) >= num_bins:
                break
            rows.append({"Shift": s, "Date": current_date.strftime("%d-%m-%Y"),
                         "Shift_Value": shift_value})
            shift_value += 1
        current_date += _timedelta(days=1)
    return {r["Shift_Value"]: r for r in rows}

def _build_sequence_for(header_dates, start_date, num_bins):
    """Picks the right sequence logic based on how many dates the plan
    header actually lists: 2 dates -> standard full-cycle-per-day;
    3 dates -> one-shift-per-day-then-pile-onto-last-date. Falls back to
    the standard logic when header_dates is unavailable (e.g. the
    /process-plan endpoint, which only receives a single start_date with
    no header context -- images have no such header at all)."""
    if header_dates and len(header_dates) == 3:
        return _build_shift_date_sequence_3day(header_dates, num_bins)
    return _build_shift_date_sequence(start_date, num_bins)

@app.post("/process-plan")
def process_plan(req: ProcessPlanRequest, include_details: bool = False):
    _require_master()
    try:
        start = _date.fromisoformat(req.start_date)
    except ValueError:
        raise HTTPException(422, "start_date must be ISO format YYYY-MM-DD")
    if not req.rows:
        raise HTTPException(422, "rows is empty")

    bin_size = req.bin_size or BIN_SIZE

    # attach master data (name + weights) to each confirmed row
    base_rows = []
    unknown_ids = []
    for r in req.rows:
        hit = _master[_master["Component ID"] == str(r.component_id)]
        if hit.empty:
            unknown_ids.append(r.component_id)
            continue
        m = hit.iloc[0]
        base_rows.append({
            "Order": r.order_no,
            "Component ID": str(r.component_id),
            "Component Name": m["Component Name"],
            "BOX": float(r.boxes),
            "_bunch": float(m["Bunch Weight (Kg)"]),
            "_core": float(m["Core Weight (Kg)"]),
            "_sand": float(m["Sand Weight (Kg)"]),
        })
    if not base_rows:
        raise HTTPException(422, f"No confirmed rows had a known Component ID. "
                                 f"Unknown: {unknown_ids}")

    binned = _split_into_bins(base_rows, bin_size)
    num_bins = max(r["Bin"] for r in binned)
    seq = _build_shift_date_sequence(start, num_bins)

    out = []
    bin_counters = {}   # per-bin Order numbering: '#' restarts 1..N in each shift-bin
    for r in binned:
        label = seq[r["Bin"]]
        bin_counters[r["Bin"]] = bin_counters.get(r["Bin"], 0) + 1
        row = {
            "Order": bin_counters[r["Bin"]],   # per-shift order number
            "Date": label["Date"],
            "Shift": label["Shift"],
            "BOX": r["BOX"],
            "Component ID": r["Component ID"],
        }
        if include_details:
            row.update({
                "Source Order": r["Order"],    # original '#' from the plan
                "Component Name": r["Component Name"],
                "Total Metal Weight (Kg)": round(r["BOX"] * r["_bunch"], 2),
                "Total Core Weight (Kg)": round(r["BOX"] * r["_core"], 2),
                "Total Sand Weight (Kg)": round(r["BOX"] * r["_sand"], 2),
            })
        out.append(row)

    return {
        "start_date": req.start_date,
        "bin_size": bin_size,
        "num_shift_bins": num_bins,
        "unknown_component_ids": unknown_ids,   # skipped rows, if any
        "row_count": len(out),
        "rows": out,
    }


if __name__ == "__main__":
    import uvicorn
    from config import API_HOST, API_PORT
    uvicorn.run(app, host=API_HOST, port=API_PORT)
