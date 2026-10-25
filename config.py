"""Shared settings for the MERRY audit app.

Change values here instead of inside the SQL in the other modules.
"""

STAGE = "@RAW_DB.PUBLIC.RAW_STAGE"   # where uploaded files land
SCHEMA = "RAW_DB.AUDIT"              # all audit tables and views live here

# Audit thresholds
HOURS_TOLERANCE = 1.0      # billed vs actual hours difference allowed before flagging
TRAVEL_CAP_HOURS = 4.0     # MSA 5.4: up to 2 h travel each way per day
VAT_RATE = 0.12            # Philippine VAT
AMOUNT_TOLERANCE = 1.0     # PHP rounding allowed in invoice math checks

# OCR quality for AI_EXTRACT (1.0 - 4.0). 2.0 fixed misread rate codes in the contract.
EXTRACT_SCALE_FACTOR = 2.0

# LLM used by the chat assistant
AGENT_MODEL = "claude-sonnet-4-5"

# CSV files we know how to load: file name -> target table.
CSV_TABLES = {
    "timesheets.csv": "TIMESHEETS",
    "daily_reports.csv": "DAILY_REPORTS",
    "equipment_log.csv": "EQUIPMENT_LOG",
    "gate_log.csv": "GATE_LOG",
    "rate_card.csv": "RATE_CARD",
}
