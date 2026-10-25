"""Step 1 - Create the database objects the app needs (safe to run many times).

Everything uses IF NOT EXISTS, so running setup again never deletes data.
"""

from config import SCHEMA

DDL = [
    f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",

    # CSV parsing: header row skipped, quoted text allowed ("PumpCo ops, 27 t")
    f"""CREATE FILE FORMAT IF NOT EXISTS {SCHEMA}.FF_CSV
        TYPE = CSV SKIP_HEADER = 1 FIELD_OPTIONALLY_ENCLOSED_BY = '"'
        TRIM_SPACE = TRUE EMPTY_FIELD_AS_NULL = TRUE""",

    # ---------- CSV tables (one per file) ----------
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.TIMESHEETS (
        work_date DATE, worker_id STRING, worker_name STRING, contractor_code STRING,
        role STRING, hours NUMBER(6,2), approved_by STRING, source_file STRING)""",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.DAILY_REPORTS (
        report_date DATE, weather STRING, ops_status STRING, camp_headcount NUMBER,
        activities STRING, supervisor_notes STRING, reported_by STRING, source_file STRING)""",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.EQUIPMENT_LOG (
        log_date DATE, equipment_id STRING, contractor_code STRING, status STRING,
        hours_run NUMBER(6,2), remarks STRING, source_file STRING)""",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.GATE_LOG (
        gate_entry_id STRING, entry_date DATE, subject_type STRING, subject_id STRING,
        subject_name STRING, contractor_code STRING, gate STRING,
        time_in TIMESTAMP_NTZ, time_out TIMESTAMP_NTZ, remarks STRING, source_file STRING)""",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.RATE_CARD (
        contractor_code STRING, contractor_name STRING, rate_code STRING, item STRING,
        unit STRING, rate_php NUMBER(12,2), standby_rate_php NUMBER(12,2),
        term_start DATE, term_end DATE, source_file STRING)""",

    # ---------- PDF extraction results (raw JSON, one row per file) ----------
    # Typed views over this table are created in audit_rules.py.
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.DOC_EXTRACTS (
        file_name STRING, doc_type STRING, extracted_at TIMESTAMP_NTZ, result VARIANT)""",

    # ---------- Reviewer decisions on flags ----------
    # status: OPEN (default) | CONFIRMED | RECOVERED | DISMISSED
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.FLAG_REVIEW (
        flag_id STRING, status STRING, note STRING, updated_at TIMESTAMP_NTZ)""",
]


def setup_database(session) -> None:
    """Create schema, file format and tables if they do not exist yet."""
    for stmt in DDL:
        session.sql(stmt).collect()
