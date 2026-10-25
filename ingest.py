"""Step 2 - Ingest files from the stage into the AUDIT tables.

CSV  -> loaded directly into a typed table (no AI needed).
PDF  -> AI_CLASSIFY reads the document and decides its type (INVOICE / TICKET / CONTRACT / OTHER),
        so file names do not matter (the old file-name rule is only a fallback if AI_CLASSIFY fails).
        Then AI_EXTRACT reads every field in ONE call per document and the raw JSON
        is saved in DOC_EXTRACTS. Typed views (TICKETS, INVOICE_LINES, ...) are
        built on top of it in audit_rules.py, so a PDF is only sent to AI once.
"""

import json
import re

from config import CSV_TABLES, EXTRACT_SCALE_FACTOR, SCHEMA, STAGE

# Accepts both 9/1/2026 and 2026-09-01
_DATE = "COALESCE(TRY_TO_DATE({c}, 'YYYY-MM-DD'), TRY_TO_DATE({c}, 'MM/DD/YYYY'))"


def _d(col: str) -> str:
    return _DATE.format(c=col)


# SELECT list for each CSV table, in the same column order as the CSV header.
CSV_SELECT = {
    "TIMESHEETS": f"{_d('$1')}, $2, $3, $4, $5, TRY_TO_NUMBER($6, 6, 2), $7",
    "DAILY_REPORTS": f"{_d('$1')}, $2, $3, TRY_TO_NUMBER($4), $5, $6, $7",
    "EQUIPMENT_LOG": f"{_d('$1')}, $2, $3, $4, TRY_TO_NUMBER($5, 6, 2), $6",
    "GATE_LOG": (f"$1, {_d('$2')}, $3, $4, $5, $6, $7, "
                 "TRY_TO_TIMESTAMP_NTZ($8, 'YYYY-MM-DD HH24:MI'), "
                 "TRY_TO_TIMESTAMP_NTZ($9, 'YYYY-MM-DD HH24:MI'), $10"),
    "RATE_CARD": (f"$1, $2, $3, $4, $5, TRY_TO_NUMBER($6, 12, 2), TRY_TO_NUMBER($7, 12, 2), "
                  f"{_d('$8')}, {_d('$9')}"),
}

# ----------------------------------------------------------------------------
# AI_EXTRACT response formats (one per PDF type). Dates are kept "as written"
# and converted in SQL, because each document type uses a different format.
# ----------------------------------------------------------------------------
def _table(description: str, columns: list[str]) -> dict:
    return {
        "type": "object",
        "description": description,
        "column_ordering": columns,
        "properties": {c: {"type": "array"} for c in columns},
    }


def _text(description: str) -> dict:
    return {"type": "string", "description": description}


EXTRACT_FORMATS = {
    "TICKET": {"schema": {"type": "object", "properties": {
        "ticket_no": _text("Field ticket number (No.)"),
        "ticket_date": _text("Ticket date exactly as written"),
        "contractor": _text("Contractor company name in the header"),
        "site": _text("Site"),
        "contract_ref": _text("Contract number"),
        "approved_by": _text("Name of the Company Representative who approved, or None if not signed/approved"),
        "remarks": _text("Remarks text, or None"),
        "personnel": _table("PERSONNEL table", ["Name", "Role", "Hours"]),
        "equipment": _table("EQUIPMENT table", ["Equipment ID", "Description", "Qty", "Unit"]),
        "quantities": _table("QUANTITIES table", ["Item", "Qty", "Unit"]),
    }}},
    "INVOICE": {"schema": {"type": "object", "properties": {
        "invoice_no": _text("Invoice No."),
        "invoice_date": _text("Invoice Date exactly as written"),
        "contractor": _text("Contractor company name in the header"),
        "period_start": _text("Service Period start date exactly as written"),
        "period_end": _text("Service Period end date exactly as written"),
        "contract_ref": _text("Contract Ref."),
        "subtotal": _text("Subtotal amount, digits only"),
        "vat": _text("VAT amount, digits only"),
        "total_due": _text("TOTAL DUE amount, digits only"),
        "lines": _table("Invoice line items table",
                        ["Date", "Ticket No.", "Description", "Code", "Qty", "Unit", "Rate", "Amount (PHP)"]),
    }}},
    "CONTRACT": {"schema": {"type": "object", "properties": {
        "agreement_no": _text("Agreement No."),
        "effective_date": _text("Effective Date of the agreement"),
        "expiry_date": _text("Expiry Date of the agreement"),
        "day_rate_hours": _text("Maximum hours covered by a day rate (Definition of Day), digits only"),
        "standby_pct": _text("Standby percentage of day rate for idle equipment, digits only"),
        "travel_cap_hours": _text("Travel time cap in hours each way per day, digits only"),
        "rates": _table("EXHIBIT A: RATE SCHEDULE, all contractor sub-tables combined",
                        ["Code", "Item", "Unit", "Rate (PHP)"]),
        "schedules": _table("EXHIBIT B: CONTRACTOR SCHEDULES",
                            ["Contractor", "Code", "Base", "Term Start", "Term End"]),
    }}},
}


# ----------------------------------------------------------------------------
# Routing helpers
# ----------------------------------------------------------------------------
def safe_name(name: str) -> str:
    """Keep only safe characters so the file name can be used in a stage path and SQL."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def csv_table_for(filename: str) -> str | None:
    """Map a CSV file name to its target table, e.g. 'gate_log_oct.csv' -> GATE_LOG."""
    stem = filename.lower()
    for known, table in CSV_TABLES.items():
        if stem.startswith(known.removesuffix(".csv").split("_")[0]):
            return table
    return None


def pdf_type_for(filename: str) -> str | None:
    """Fallback: detect the document type from the file name (used only if AI_CLASSIFY fails)."""
    name = filename.upper()
    if "-INV-" in name or "INVOICE" in name:
        return "INVOICE"
    if "-T-" in name or "TICKET" in name:
        return "TICKET"
    if "CONTRACT" in name or "MSA" in name:
        return "CONTRACT"
    return None


# Labels for AI_CLASSIFY. Each label must also be a key in EXTRACT_FORMATS (except OTHER).
# Descriptions (max 25 words each) tell the model what makes each type different.
DOC_TYPE_LABELS = [
    {"label": "INVOICE", "description": "Contractor bill requesting payment, with invoice number, line items, amounts, VAT and total due"},
    {"label": "TICKET", "description": "Daily field ticket recording personnel, hours, equipment and quantities at a site, approved by a company representative"},
    {"label": "CONTRACT", "description": "Master service agreement or contract with terms, clauses, rate schedule and contractor schedules"},
    {"label": "OTHER", "description": "Any other document"},
]
CLASSIFY_CONFIG = {"task_description": "Classify oilfield contractor documents by document type"}


def _sql_literal(value) -> str:
    """Render a Python constant as a Snowflake array/object constant: {'k': 'v'}, ['a', 'b'].

    AI_CLASSIFY needs its config as a constant (PARSE_JSON(?) is rejected). Only use this for
    the module constants above - never for user input.
    """
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_sql_literal(k)}: {_sql_literal(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_sql_literal(v) for v in value) + "]"
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def classify_pdf(session, filename: str) -> tuple[str | None, str]:
    """Detect the document type from its CONTENT with AI_CLASSIFY.

    Returns (doc_type, how) where how is 'AI_CLASSIFY' or 'file name'. If AI_CLASSIFY errors,
    falls back to the file-name rule so ingestion still works.
    """
    try:
        row = session.sql(
            f"""SELECT AI_CLASSIFY(TO_FILE('{STAGE}', ?), {_sql_literal(DOC_TYPE_LABELS)},
                                   {_sql_literal(CLASSIFY_CONFIG)}, TRUE)""",
            params=[filename],
        ).collect()[0][0]
        result = json.loads(row) if row else {}
    except Exception:
        result = {}
    labels = (result.get("value") or {}).get("labels") or []
    if labels:
        label = labels[0]
        return (label if label in EXTRACT_FORMATS else None), "AI_CLASSIFY"
    return pdf_type_for(filename), "file name"


# ----------------------------------------------------------------------------
# Loaders
# ----------------------------------------------------------------------------
def load_csv(session, filename: str) -> str:
    """Replace this file's rows in its table with the current file contents."""
    filename = safe_name(filename)
    table = csv_table_for(filename)
    if table is None:
        return f"⚠️ {filename} → skipped: unknown CSV type (name should start with timesheets, daily, equipment, gate or rate)"

    pattern = "(.*/)?" + re.escape(filename).replace(r"\.", "[.]")
    session.sql(f"DELETE FROM {SCHEMA}.{table} WHERE source_file = ?", params=[filename]).collect()
    session.sql(
        f"""INSERT INTO {SCHEMA}.{table}
            SELECT {CSV_SELECT[table]}, METADATA$FILENAME
            FROM {STAGE} (FILE_FORMAT => '{SCHEMA}.FF_CSV', PATTERN => '{pattern}')"""
    ).collect()
    n = session.sql(f"SELECT COUNT(*) FROM {SCHEMA}.{table} WHERE source_file = ?",
                    params=[filename]).collect()[0][0]
    return f"📊 {filename} → loaded {n} rows into {table.replace('_', ' ').title()}"


def extract_pdf(session, filename: str) -> str:
    """Classify the PDF, then run AI_EXTRACT once with that type's format and store the JSON result."""
    filename = safe_name(filename)
    doc_type, how = classify_pdf(session, filename)
    if doc_type is None:
        return f"⚠️ {filename} → skipped: not an invoice, field ticket or contract"

    extract_sql = f"""AI_EXTRACT(
            file => TO_FILE('{STAGE}', ?),
            responseFormat => PARSE_JSON(?),
            config => PARSE_JSON(?))"""
    params = [filename, json.dumps(EXTRACT_FORMATS[doc_type]),
              json.dumps({"scale_factor": EXTRACT_SCALE_FACTOR})]

    # Contracts also keep their full text so the chat assistant can quote clauses.
    if doc_type == "CONTRACT":
        extract_sql = f"""OBJECT_INSERT({extract_sql}, 'text',
            AI_PARSE_DOCUMENT(TO_FILE('{STAGE}', ?), {{'mode': 'LAYOUT'}}):content)"""
        params.append(filename)

    session.sql(f"DELETE FROM {SCHEMA}.DOC_EXTRACTS WHERE file_name = ?", params=[filename]).collect()
    session.sql(
        f"""INSERT INTO {SCHEMA}.DOC_EXTRACTS (file_name, doc_type, extracted_at, result)
            SELECT ?, ?, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ, {extract_sql}""",
        params=[filename, doc_type, *params],
    ).collect()
    err = session.sql(f"SELECT result:error::STRING FROM {SCHEMA}.DOC_EXTRACTS WHERE file_name = ?",
                      params=[filename]).collect()[0][0]
    return f"📄 {filename} → classified as {doc_type.title()} and extracted" + (f" (warning: {err})" if err else "")


def ingest_staged_file(session, filename: str) -> str:
    """Route one file that is already on the stage to the right loader."""
    if filename.lower().endswith(".csv"):
        return load_csv(session, filename)
    if filename.lower().endswith(".pdf"):
        return extract_pdf(session, filename)
    return f"Skipped {filename}: only .csv and .pdf are supported"


def ingest_upload(session, uploaded_file) -> str:
    """Upload a Streamlit file to the stage, then ingest it."""
    filename = upload_to_stage(session, uploaded_file)
    session.sql(f"ALTER STAGE {STAGE[1:]} REFRESH").collect()
    return ingest_staged_file(session, filename)


def upload_to_stage(session, uploaded_file) -> str:
    """Put a Streamlit upload on the stage (overwrites a file with the same name). Returns the stage file name."""
    filename = safe_name(uploaded_file.name)
    session.file.put_stream(uploaded_file, f"{STAGE}/{filename}", auto_compress=False, overwrite=True)
    return filename


def list_stage_files(session) -> list[str]:
    session.sql(f"ALTER STAGE {STAGE[1:]} REFRESH").collect()
    rows = session.sql(f"SELECT RELATIVE_PATH FROM DIRECTORY({STAGE}) ORDER BY 1").collect()
    return [r[0] for r in rows]


def processed_files(session) -> set[str]:
    """Files already extracted (PDF) or loaded (CSV)."""
    tables = ["DOC_EXTRACTS"] + list(dict.fromkeys(CSV_TABLES.values()))
    union = " UNION ".join(
        f"SELECT {'file_name' if t == 'DOC_EXTRACTS' else 'source_file'} AS f FROM {SCHEMA}.{t}" for t in tables)
    return {r[0] for r in session.sql(union).collect()}


def submit(session, uploaded_files, on_progress=None) -> list[str]:
    """One-click ingestion used by the Submit button.

    1. Upload the selected files to the stage (always re-processed, so a re-upload replaces old data).
    2. Also process any other stage file that was never loaded/extracted (first-time setup).
    Files already processed and not re-uploaded are left alone, so no AI credits are spent twice.
    on_progress(message) is called after each file so the UI can show progress live.
    """
    uploaded = [upload_to_stage(session, f) for f in uploaded_files or []]
    done = processed_files(session)
    on_stage = list_stage_files(session)          # also refreshes the stage directory
    todo = uploaded + [f for f in on_stage if f not in done and f not in uploaded]
    messages = []
    for f in todo:
        messages.append(ingest_staged_file(session, f))
        if on_progress:
            on_progress(messages[-1])
    return messages


def ingest_all(session, skip_extracted_pdfs: bool = True) -> list[str]:
    """(Re)load every file on the stage. PDFs already extracted are skipped by default to save AI cost."""
    done = set()
    if skip_extracted_pdfs:
        done = {r[0] for r in session.sql(f"SELECT file_name FROM {SCHEMA}.DOC_EXTRACTS").collect()}
    messages = []
    for f in list_stage_files(session):
        if f.lower().endswith(".pdf") and f in done:
            messages.append(f"Already extracted {f}")
            continue
        messages.append(ingest_staged_file(session, f))
    return messages
