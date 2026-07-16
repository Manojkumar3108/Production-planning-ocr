"""
Gemini-only board extractor for Sandman production plans.

Reads every board photo in IMAGES_FOLDER with Google Gemini vision,
fuzzy-matches the handwritten item names against the component master
(with the MPM board-shorthand rules: J.D / F/C / D/C / Esc), and writes
one combined Excel with a Source File column.

Dependencies (no OpenCV / Paddle / EasyOCR needed):
    pip install google-generativeai pillow pandas rapidfuzz openpyxl

Run:
    python gemini_board_extractor.py
"""

import os
import re
import json
import time
import glob
import traceback

import pandas as pd
from PIL import Image
from rapidfuzz import process, fuzz
import google.generativeai as genai

# ===========================================================
# CONFIGURATION
#
# ALL settings live in config.py, which must sit in the same folder.
# There are no defaults here -- config.py is the single source of truth.
# ===========================================================

try:
    from config import (
        IMAGES_FOLDER, MASTER_FILE, COMPONENT_COLUMN, OUTPUT_FILE,
        GEMINI_MODEL, API_KEYS, MATCH_THRESHOLD, MATCH_LIMIT, DEBUG,
    )
except ImportError as e:
    raise SystemExit(
        "config.py not found (or missing a setting). It must sit in the same "
        "folder as this script and define: IMAGES_FOLDER, MASTER_FILE, "
        "COMPONENT_COLUMN, OUTPUT_FILE, GEMINI_MODEL, API_KEYS, "
        f"MATCH_THRESHOLD, MATCH_LIMIT, DEBUG.\nDetails: {e}"
    )

IMAGE_EXTENSIONS = ("*.png", "*.jpg", "*.jpeg", "*.bmp", "*.webp")

# ===========================================================
# GEMINI EXTRACTION
# ===========================================================

_PROMPT = """This photo shows a handwritten foundry production-plan whiteboard. It is a table whose data columns include a serial number (Sr.No), a handwritten Item Name, and a Plan quantity. Some boards have extra columns (Cls, Act, Rej, Bal) and some have no Sr.No column - ignore extra columns.

Transcribe every DATA row. Rules:
- "sr_no": the row's serial number as an integer (they run 1,2,3... top to bottom; if unclear or absent, use the row's position).
- "item_name": the item name EXACTLY as written, preserving abbreviations like Esc, J.D, F/C, D/C. Do not expand or correct them.
- "plan": the Plan quantity as a string of digits, or "??" if that cell is empty or unreadable.
- EXCLUDE the board title, shift/date line, the printed column-header row, the "Total" row, and rows with no item written.
Respond with ONLY a JSON array like [{"sr_no":1,"item_name":"Esc 3FT","plan":"61"}] - no markdown fences, no commentary."""

def gemini_call_with_rotation(prompt_parts):
    if not API_KEYS:
        raise RuntimeError(
            "No Gemini API keys configured. Set API_KEYS in config.py or the "
            "environment variable SANDMAN_GEMINI_KEYS (comma-separated)."
        )
    last_err = None
    for key in API_KEYS:
        try:
            if DEBUG:
                print(f"  Gemini call with key ...{key[-4:]}")
            genai.configure(api_key=key)
            model = genai.GenerativeModel(GEMINI_MODEL)
            response = model.generate_content(prompt_parts,
                                              request_options={"timeout": 120})
            return response.text
        except Exception as e:
            last_err = e
            if DEBUG:
                print(f"  Key ...{key[-4:]} failed: {e} -- trying next key")
            time.sleep(1)
    raise RuntimeError(f"All Gemini API keys failed. Last error: {last_err}")

# ===========================================================
# PLAN NUMBER NORMALIZATION + TOTAL SAFETY NET
# (kept from the pipeline -- the prompt asks for clean values, but these
#  guards cost nothing and catch the occasional slip)
# ===========================================================

_DIGIT_LOOKALIKES = str.maketrans({
    'O': '0', 'o': '0', 'Q': '0',
    'I': '1', 'l': '1', '|': '1', 'i': '1', '(': '1',
    'Z': '2', 'z': '2', 'S': '5', 's': '5', 'B': '8', 'G': '6',
})

def extract_plan_number(text):
    t = str(text).strip().translate(_DIGIT_LOOKALIKES)
    m = re.search(r"\d{1,4}", t)
    return m.group(0) if m else ""

_LETTER_LOOKALIKES = str.maketrans({'0': 'O', '1': 'I', '5': 'S', '7': 'T',
                                    '8': 'B', '2': 'Z', '6': 'G'})

def is_total_row(texts, plan=""):
    plan_missing = plan in ("", "??")
    for text in texts:
        toks = [re.sub(r"[^A-Z0-9]", "", t)
                for t in re.split(r"\s+", str(text).upper())]
        toks = [t for t in toks if t]
        for tok in toks:
            fixed = tok.translate(_LETTER_LOOKALIKES)
            r = fuzz.ratio(fixed, "TOTAL")
            if len(fixed) >= 4 and r >= 80:
                return True
            if len(fixed) >= 4 and r >= 55 and len(toks) <= 2 and plan_missing:
                return True
    return False

def extract_rows(image_path):
    """Gemini reads the board; returns [{'Sr.No','Item Name','Item Candidates','Plan'}]."""
    img = Image.open(image_path)
    text = gemini_call_with_rotation([_PROMPT, img])
    cleaned = re.sub(r"```(json)?", "", text).strip()
    parsed = json.loads(cleaned)
    if not isinstance(parsed, list):
        raise ValueError("Gemini response was not a JSON array")

    records = []
    for i, r in enumerate(parsed, start=1):
        item = str(r.get("item_name", "")).strip()
        if not item:
            continue
        plan_raw = str(r.get("plan", "??")).strip()
        plan = plan_raw if plan_raw == "??" else (extract_plan_number(plan_raw) or "??")
        if is_total_row([item], plan):
            continue
        records.append({
            "Sr.No": str(r.get("sr_no", i)),
            "Item Name": item,
            "Item Candidates": [item],
            "Plan": plan,
        })
    return records

# ===========================================================
# NORMALIZATION FOR MATCHING (MPM board-shorthand rules)
# ===========================================================

def clean(text):
    text = str(text).upper()
    # merge compound board shorthand BEFORE punctuation stripping splits it
    text = re.sub(r"\bJ[\.\s]*D\b", "JD", text)
    text = re.sub(r"\bF\s*/\s*C\b", "FC", text)
    text = re.sub(r"\bD\s*/\s*C\b", "DC", text)
    text = text.replace(".", " ").replace("-", " ").replace("/", " ")
    text = re.sub(r"[^A-Z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()

ABBREVIATIONS = {
    "ESC": "ESCORTS",
    "HSG": "HOUSING",
    "TRANS": "TRANSMISSION",
    "FC": "CASE FRONT",   # board shorthand F/C
    "DC": "CASE DIFF",    # board shorthand D/C
}

# Handwritten-side only: OCR/vision misreads of board shorthand
JD_LEAD_VARIANTS = {"7D", "1D", "ID", "J0", "JO", "TD", "J", "JA", "JB"}
QUERY_TOKEN_FIXES = {
    "FLC": "CASE FRONT", "F1C": "CASE FRONT", "FIC": "CASE FRONT",
    "DLC": "CASE DIFF", "D1C": "CASE DIFF", "PLC": "CASE DIFF", "PKC": "CASE DIFF",
    "JDLC": "JD CASE DIFF", "JPLC": "JD CASE DIFF", "JDIC": "JD CASE DIFF",
    "ESE": "ESCORTS", "FSC": "ESCORTS", "FSE": "ESCORTS", "ESO": "ESCORTS",
    "E5C": "ESCORTS",
}

def expand_abbrev(text):
    return " ".join(ABBREVIATIONS.get(t, t) for t in text.split())

def strip_part_codes(text):
    return " ".join(t for t in text.split() if not re.fullmatch(r"\d{6,}", t))

def normalize_for_match(text):
    return expand_abbrev(strip_part_codes(clean(text)))

def normalize_query(text):
    toks = clean(text).split()
    if toks and toks[0] in JD_LEAD_VARIANTS:
        toks[0] = "JD"
    toks = [QUERY_TOKEN_FIXES.get(t, t) for t in toks]
    return expand_abbrev(strip_part_codes(" ".join(toks)))

# ===========================================================
# MATCH SCORER (token_set + token_sort + char-tolerant token alignment)
# ===========================================================

def _token_align(q, m):
    qt, mt = q.split(), m.split()
    if not qt or not mt:
        return 0.0
    total_w, total = 0.0, 0.0
    for t in qt:
        best = max(fuzz.ratio(t, u) for u in mt)
        w = len(t)
        total += best * w
        total_w += w
    return total / total_w

def match_score(query, candidate, **kwargs):
    return 0.35 * fuzz.token_set_ratio(query, candidate) \
         + 0.20 * fuzz.token_sort_ratio(query, candidate) \
         + 0.45 * _token_align(query, candidate)

# ===========================================================
# MATCHING
#
# Returns one dict per component (instead of flat rows with blank
# continuation lines): all candidate matches live inside the "matches"
# list, best first.
# ===========================================================

def match_records(records, clean_master, master):
    results = []
    for row in records:
        candidates = row.get("Item Candidates") or [row["Item Name"]]

        best_matches = []
        best_query_text = row["Item Name"]
        best_top_score = -1
        for cand in candidates:
            q = normalize_query(cand)
            if not q:
                continue
            m = process.extract(q, clean_master, scorer=match_score, limit=MATCH_LIMIT)
            m = [x for x in m if x[1] >= MATCH_THRESHOLD]
            top = m[0][1] if m else 0
            if top > best_top_score:
                best_top_score = top
                best_matches = m
                best_query_text = cand

        results.append({
            "order_no": row["Sr.No"],
            "extracted_name": best_query_text,   # item exactly as written on the board
            "matches": [
                {"name": str(master.iloc[idx][COMPONENT_COLUMN]),
                 "score": round(score, 1)}
                for _, score, idx in best_matches
            ],
            "box": row["Plan"],
        })
    return results

# ===========================================================
# MAIN BATCH LOOP
# ===========================================================

def main():
    master = pd.read_excel(MASTER_FILE, skiprows=5).fillna("")
    if COMPONENT_COLUMN not in master.columns:
        raise KeyError(
            f"'{COMPONENT_COLUMN}' not found in master file columns.\n"
            f"Available columns: {list(master.columns)}"
        )
    master["Clean"] = master[COMPONENT_COLUMN].astype(str).apply(normalize_for_match)
    clean_master = master["Clean"].tolist()

    # IMAGES_FOLDER may be a folder (all images inside are processed) or a
    # single image file (just that one is processed)
    if os.path.isfile(IMAGES_FOLDER):
        image_paths = [IMAGES_FOLDER]
    else:
        image_paths = []
        for pattern in IMAGE_EXTENSIONS:
            image_paths.extend(glob.glob(os.path.join(IMAGES_FOLDER, pattern)))
        image_paths = sorted(set(image_paths))
    if not image_paths:
        raise FileNotFoundError(f"No images found at {IMAGES_FOLDER}")

    print(f"Gemini model: {GEMINI_MODEL}")
    print(f"Found {len(image_paths)} images in {IMAGES_FOLDER}\n")

    all_results = []

    for idx, path in enumerate(image_paths, start=1):
        name = os.path.basename(path)
        print(f"[{idx}/{len(image_paths)}] {name}")
        try:
            records = extract_rows(path)
            if not records:
                print("  -> no rows extracted from this image")
            else:
                all_results.extend(match_records(records, clean_master, master))
                print(f"  -> {len(records)} components extracted")
        except Exception as e:
            print(f"  -> ERROR: {e}")
            if DEBUG:
                traceback.print_exc()

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)
    print(f"\nSaved {len(all_results)} components from {len(image_paths)} images -> {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
