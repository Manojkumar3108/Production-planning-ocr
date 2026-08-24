"""
Central configuration for the Sandman board-extraction tools.

Both gemini_board_extractor.py and sandman_extraction_api.py read their
settings from here (falling back to built-in defaults if this file is
missing). Environment variables still take highest priority where noted,
so deployments can override without editing files.
"""

import os
from dotenv import load_dotenv
load_dotenv()
# ===========================================================
# DATABASE -- same three environments as DB Acess/config.py
# (DevelopmentConfig / StgConfig / ProductionConfig), selected here by
# SANDMAN_DB_ENV instead of Flask's APP_SETTINGS. Defaults to production,
# the RDS instance this service has been pointed at.
# ===========================================================
DB_ENVIRONMENTS = {
    "development": {
        "host": "localhost",
        "port": 3307,
        "name": "sandman_dev",
        "user": "root",
        "password": "sandman",
    },
    "staging": {
        "host": "127.0.0.1",
        "port": 3306,
        "name": "sandman_dev",
        "user": "root",
        "password": "S@ndm@Ndb_2020&stg",
    },
    "production": {
        "host": "sandman.cjmg6ek4cf9c.ap-south-1.rds.amazonaws.com",
        "port": 43306,
        "name": "sandman_dev",
        "user": "admin",
        "password": "$anDman!2025&",
    },
}

# Switch environments by changing this value directly.
_db_env = "development"
_db_cfg = DB_ENVIRONMENTS[_db_env]

DB_HOST = _db_cfg["host"]
DB_PORT = _db_cfg["port"]
DB_NAME = _db_cfg["name"]
DB_USER = _db_cfg["user"]
DB_PASSWORD = _db_cfg["password"]

DB_TABLE = "components"

DB_COLUMN_MAP = {
    "component_id":   "component_id",
    "component_name": "component_name",
    "bunch_weight":   "bunch_weight",
    "core_weight":    "core_weight",
    "sand_weight":    "sand_weight",
}

# Auto-refresh: re-query the master from MySQL on this schedule. Minutes;
# 0 disables it (refresh only at startup / manual POST /master/refresh-from-db).
DB_REFRESH_INTERVAL_MINUTES = int(os.environ.get("SANDMAN_DB_REFRESH_MINUTES", "60"))
# Board photos to process when running gemini_board_extractor.py directly
# as a batch script. Can be a FOLDER (all images inside are processed) or a
# SINGLE image file (e.g. r"D:\GPI OCR\Images\board1.jpg").
IMAGES_FOLDER = r"D:\GPI OCR\Images"

# Component master Excel (read with skiprows=5). Must contain
# COMPONENT_COLUMN and "Component ID"; the weight columns below are needed
# by the API's /process-plan endpoint (missing ones compute as 0).
MASTER_FILE = "Component_01-Jan-2015_TO_09-Jul-2026_SAVELLI.xlsx"
COMPONENT_COLUMN = "Component Name"
WEIGHT_COLUMNS = ["Bunch Weight (Kg)", "Core Weight (Kg)", "Sand Weight (Kg)"]

# Output of the standalone batch script (JSON:
# [{"order_no", "matches":[{"name","score"}], "box"}, ...])
OUTPUT_FILE = "Matched_Components_ALL.json"

# ===========================================================
# GEMINI
# ===========================================================

# Env var SANDMAN_GEMINI_MODEL overrides.
GEMINI_MODEL = os.environ.get("SANDMAN_GEMINI_MODEL", "gemini-3-flash-preview")

# API keys, comma-separated. Env var SANDMAN_GEMINI_KEYS overrides.
# NOTE: these default keys have circulated in shared script files --

API_KEYS = [k.strip() for k in
            os.environ.get("SANDMAN_GEMINI_KEYS","").split(",")
            if k.strip()]

# ===========================================================
# MATCHING
# ===========================================================

MATCH_THRESHOLD = 40   # minimum fuzzy score for a candidate to be listed
MATCH_LIMIT = 10       # candidates listed per component

# ===========================================================
# PLAN PROCESSING (API /process-plan)
# ===========================================================

BIN_SIZE = 500         # boxes per shift-bin

# ===========================================================
# SERVICE
# ===========================================================

API_HOST = "0.0.0.0"
API_PORT = 8077

DEBUG = True
