EXTRACTION API - DEPLOYMENT

CONTENTS
--------
config.py                   All settings (THE ONLY FILE YOU EDIT)
gemini_board_extractor.py   Gemini vision extraction + fuzzy matching library
                            (also runs standalone on a folder/single image)
sandman_extraction_api.py   REST API for Sandman (port 8077)
requirements.txt            Python dependencies

NOT included (bring your own): the component master Excel
(Component_..._SAVELLI.xlsx). Place it in this same folder and make sure
config.py's MASTER_FILE matches its filename exactly.


SETUP (once)
------------
1. Unzip everything into one folder, e.g. D:\sandman_api\
2. Copy the component master Excel into the same folder.
3. Open config.py and check:
     - MASTER_FILE matches the master's filename
     - API_KEYS has the current Gemini keys
     - API_PORT (default 8077) is free on this machine
4. Install dependencies (Command Prompt):
     python -m pip install -r requirements.txt


LOCAL CHECK BEFORE PRODUCTION: MASTER FROM SANDMAN DB
----------------------------------
The Excel master is only a local-testing bootstrap. In production Sandman
pushes the master from its database:

  POST /master   (JSON)
  {"rows": [{"component_id": "51011280010",
             "component_name": "ESCORTS CH 3FT 51011280010",
             "bunch_weight": 12.0, "core_weight": 2.0,
             "sand_weight": 300.0}, ...]}

- Replaces the in-memory master entirely; takes effect immediately.
- Push on API startup and whenever components change in Sandman.
- If no Excel exists AND nothing was pushed, /extract and /process-plan
  return 503 with a clear message. /health shows master_rows and
  master_source (excel | db_push | empty).
- Weights are optional (default 0) but needed for
  /process-plan?include_details=true weight totals.


--------------------------------------
1. Start the API:
     cd D:\sandman_api
     python sandman_extraction_api.py
   Expect: "Uvicorn running on http://0.0.0.0:8077". Leave the window open.

2. Health check -- browser: http://localhost:8077/health
   Expect: {"status":"ok","master_rows":<thousands>,"gemini_model":"..."}
   If master_rows looks tiny, the wrong master file is being read.

3. Interactive test page -- browser: http://localhost:8077/docs

4. IMAGE test: expand POST /extract -> Try it out -> upload a board photo
   -> Execute. Expect file_type "image" and rows with order_no /
   source_text / boxes / matches (component_id + name + score).
   NOTE: this is the first live Gemini call in this exact wiring --
   takes several seconds per image.

5. EXCEL test: same endpoint, upload a work-order xlsx.
   The full pipeline runs in one shot (ID resolution + 500-box shift
   binning + shift/date sequence). Expect file_type "spreadsheet" and rows:
     {"Order": 1, "Date": "15-07-2026", "Shift": "3",
      "BOX": 75, "Component ID": "51010350010"}
   A row bigger than 500 boxes splits across shifts (Shift 3 on the plan
   date, then 1/2/3 per following day). Order restarts 1..N per shift.
   Component ID is null when neither the GPI code nor the item name
   resolves confidently -- those rows need manual attention.
   skipped_rows lists any rows without a numeric BOX.

6. PROCESS-PLAN test: expand POST /process-plan -> Try it out -> body:
     {
       "start_date": "2026-07-14",
       "rows": [
         {"order_no": "1", "component_id": "<real id from step 5>", "boxes": 610}
       ]
     }
   Expect rows with exactly: Order, Date, Shift, BOX, Component ID.
   610 boxes should split 500 (Shift 3, start date) + 110 (Shift 1, next day).
   Add ?include_details=true to also get Source Order, Component Name,
   and the three Total ... Weight (Kg) columns.

7. Standalone batch (optional): set IMAGES_FOLDER in config.py to a folder
   or a single image, then:  python gemini_board_extractor.py
   Output: Matched_Components_ALL.json
   [{"order_no", "extracted_name", "matches":[{"name","score"}], "box"}]


PRODUCTION: MASTER FROM SANDMAN DB
----------------------------------
The Excel master is only a local-testing bootstrap. In production Sandman
pushes the master from its database:

  POST /master   (JSON)
  {"rows": [{"component_id": "51011280010",
             "component_name": "ESCORTS CH 3FT 51011280010",
             "bunch_weight": 12.0, "core_weight": 2.0,
             "sand_weight": 300.0}, ...]}

- Replaces the in-memory master entirely; takes effect immediately.
- Push on API startup and whenever components change in Sandman.
- If no Excel exists AND nothing was pushed, /extract and /process-plan
  return 503 with a clear message. /health shows master_rows and
  master_source (excel | db_push | empty).
- Weights are optional (default 0) but needed for
  /process-plan?include_details=true weight totals.



-------------------
- Sandman calls http://<this-machine-ip>:8077/extract (multipart, field
  "file") and /process-plan (JSON). The interactive docs page at /docs is
  the living contract.
- Windows Firewall will prompt to allow Python on the first external
  request -- allow it.
- Before production: tighten CORS allow_origins in sandman_extraction_api.py
  to the Sandman host, and run the API as a service (NSSM) so it survives
  reboots.

REMINDERS
---------
- The Gemini keys in config.py have circulated in shared files -- regenerate
  them in Google AI Studio and update config.py (or set the
  SANDMAN_GEMINI_KEYS environment variable, which overrides config).
- PDF uploads are rejected with a clear message by design; the UI's accept
  list should drop PDF, or ask for the PDF path to be implemented.
