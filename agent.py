"""Step 4 - Chat assistant that answers questions from ALL the audit data.

How it answers a question:
  1. The LLM writes ONE read-only SELECT against the RAW_DB.AUDIT tables/views (it is
     given the column list, so it knows the real names and types).
  2. The app checks the SQL is a single SELECT, runs it, and caps the result at 200 rows.
  3. The LLM writes the answer from: the query result + open audit flags + contract text.
Numbers therefore come from SQL, not from the model's memory of the documents.
"""

import json
import re

from config import AGENT_MODEL, SCHEMA

MAX_ROWS = 200
CONTRACT_CHARS = 15000   # contract text passed to the model (the MSA is ~9k chars)

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|GRANT|REVOKE|CALL|COPY|PUT|GET|REMOVE|"
    r"TRUNCATE|USE|EXECUTE|UNDROP|SET|UNSET|BEGIN|COMMIT|ROLLBACK)\b", re.IGNORECASE)

TABLE_NOTES = """
Notes on the data:
- TIMESHEETS: actual hours worked per person per day (from timesheets.csv).
- TICKET_PERSONNEL / TICKET_QUANTITIES / TICKETS: what the contractor billed on each field ticket (PDF).
- INVOICES / INVOICE_LINES: invoice headers and line items (PDF). amount = qty x rate, PHP excl. VAT.
- CONTRACT_RATES / CONTRACT_TERMS: Exhibit A rates and Exhibit B contractor terms from the MSA (PDF).
- RATE_CARD: rate card CSV (may differ from the contract).
- GATE_LOG: gate entries/exits (persons and equipment). GATE_PERSON_HOURS: hours on site per person per day.
- DAILY_REPORTS: weather, ops_status (NORMAL / SUSPENDED), activities, supervisor notes.
- EQUIPMENT_LOG: equipment status per day (OPERATING / IDLE / OFF_HIRE / DEMOBILIZED) and hours run.
- AUDIT_FLAGS: every issue found by the audit rules, with impact_php and reason. status OPEN/CONFIRMED/RECOVERED/DISMISSED.
- KPI_SUMMARY: one row with dashboard totals.
- contractor_code: PCS=PumpCo, LCR=Liftmax, AQH=AquaHaul, KCS=Kusina Camp, TLW=TruLine.
- Join people by UPPER(worker_name) and date.
- IMPORTANT - invoice lines vs tickets: one invoice line can cover SEVERAL tickets
  (INVOICE_LINES.ticket_ref like 'PCS-T-7101 to PCS-T-7104'; INVOICE_LINES.ticket_no is then NOT a real ticket).
  NEVER join INVOICE_LINES.ticket_no to tickets. Use:
    INVOICE_LINE_TICKETS: one row per (invoice line, ticket) with ticket_no, ticket_date, work_date,
      ticket_found, n_tickets, qty, rate, amount (qty/amount are for the WHOLE line).
    LINE_TICKET_SUPPORT: same rows plus supported_qty = units that ticket supports for that line
      (equipment days, crew of that role, tonnes, loads). Compare SUM(supported_qty) per line with qty.
- SITE_QUANTITIES: tonnes (unit_key 'T') and loads (unit_key 'L') per contractor per day from the equipment
  log and daily reports - independent of the contractor. Compare with TICKET_QUANTITIES (unit 't' / 'loads').
"""


def _complete(session, prompt: str) -> str:
    out = session.sql("SELECT AI_COMPLETE(?, ?)", params=[AGENT_MODEL, prompt]).collect()[0][0] or ""
    # AI_COMPLETE returns the text as a JSON string literal ("...\\n...") - decode it.
    if out.startswith('"'):
        try:
            out = json.loads(out)
        except json.JSONDecodeError:
            pass
    return out.strip()


def schema_description(session) -> str:
    """Column list for every table/view in the AUDIT schema (skips the per-rule R_ views)."""
    db, sch = SCHEMA.split(".")
    rows = session.sql(
        f"""SELECT table_name, LISTAGG(column_name || ' ' || data_type, ', ')
                   WITHIN GROUP (ORDER BY ordinal_position)
            FROM {db}.INFORMATION_SCHEMA.COLUMNS
            WHERE table_schema = ? AND table_name NOT LIKE 'R\\\\_%' AND table_name NOT IN ('DOC_EXTRACTS', 'CONTRACT_TEXT')
            GROUP BY table_name ORDER BY table_name""",
        params=[sch],
    ).collect()
    return "\n".join(f"{SCHEMA}.{t}({cols})" for t, cols in rows) + "\n" + TABLE_NOTES


def _clean_sql(text: str) -> str | None:
    """Pull the SQL out of the model reply and make sure it is a single read-only SELECT."""
    if not text:
        return None
    sql = re.sub(r"```(?:sql)?", "", text, flags=re.IGNORECASE).strip().rstrip(";").strip()
    if sql.upper() == "NONE" or ";" in sql:
        return None
    if not re.match(r"^(SELECT|WITH)\b", sql, re.IGNORECASE) or _FORBIDDEN.search(sql):
        return None
    return sql


def generate_sql(session, question: str, schema: str, previous_error: str = "", previous_sql: str = "") -> str | None:
    retry = (f"\nYour previous SQL failed. Fix it using ONLY columns listed above.\n"
             f"Previous SQL:\n{previous_sql}\nError: {previous_error}\n") if previous_error else ""
    prompt = f"""You write Snowflake SQL for an invoice audit app.
Tables (always use fully qualified names; use ONLY the columns listed):
{schema}

Write ONE read-only SELECT statement that returns the data needed to answer the question.
Rules: Snowflake syntax only; no semicolons; no DML/DDL; prefer aggregated results; include
contractor_code, dates and source document columns when useful. Start from the table the question
is about and LEFT JOIN related tables, so rows are not lost when there is no match.
If the question cannot be answered from these tables (e.g. it is about contract wording), reply NONE.
Reply with the SQL only, no explanation.{retry}

Question: {question}"""
    return _clean_sql(_complete(session, prompt))


def run_sql(session, sql: str):
    """Run generated SQL wrapped in a subquery (forces a plain query) with a row cap."""
    return session.sql(f"SELECT * FROM ({sql}) LIMIT {MAX_ROWS}").to_pandas()


def query_data(session, question: str, schema: str, attempts: int = 2):
    """Generate + run SQL, retrying once with the Snowflake error. Returns (sql, dataframe|None, error|None)."""
    sql, error = None, None
    for _ in range(attempts):
        sql = generate_sql(session, question, schema, error or "", sql or "")
        if not sql:
            return None, None, None
        try:
            return sql, run_sql(session, sql), None
        except Exception as e:
            error = " ".join(str(e).split())[:400]
    return sql, None, error


def ask(session, question: str, schema: str) -> dict:
    """Answer a question. Returns {'answer', 'sql', 'data', 'error'} so the UI can show its work."""
    sql, data, error = query_data(session, question, schema)
    flags = session.sql(
        f"""SELECT rule, contractor_code, source_doc, subject, work_date, billed_value, actual_value,
                   impact_php, reason
            FROM {SCHEMA}.AUDIT_FLAGS WHERE status = 'OPEN' ORDER BY impact_php DESC NULLS LAST LIMIT 100"""
    ).to_pandas()
    contract = session.sql(f"SELECT LISTAGG(text, '\\n\\n') FROM {SCHEMA}.CONTRACT_TEXT").collect()[0][0] or ""

    query_block = (data.to_csv(index=False) if data is not None and not data.empty
                   else "(no rows)" if data is not None else f"(no query result{': ' + error if error else ''})")

    prompt = f"""You are MERRY, an invoice audit assistant for Pinnacle Upstream Philippines.
Answer the question using ONLY the information below. Amounts are PHP. Be concise and specific:
cite document names, ticket numbers, dates and contract sections. If the data does not answer it, say so.
An empty query result only means that specific query matched nothing - do not claim more than that.

QUESTION: {question}

QUERY RESULT (from SQL: {sql or 'none'}):
{query_block}

OPEN AUDIT FLAGS:
{flags.to_csv(index=False) if not flags.empty else '(none)'}

CONTRACT TEXT (MSA):
{contract[:CONTRACT_CHARS]}
"""
    return {"answer": _complete(session, prompt), "sql": sql, "data": data, "error": error}


def explain_flag(session, flag: dict) -> str:
    """One-paragraph plain-language explanation of a single flag for a reviewer."""
    prompt = ("Explain this invoice audit finding to a non-technical reviewer in 2-3 sentences, "
              "including what to check and which contract clause applies:\n"
              + json.dumps(flag, default=str))
    return _complete(session, prompt)
