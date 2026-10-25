# MERRY – Documentation

- [Architecture](#architecture)
- [Ingestion](#ingestion)
- [CSV formats](#csv-formats)
- [Data model](#data-model)
- [Audit rules](#audit-rules)
- [Review workflow and KPIs](#review-workflow-and-kpis)
- [Chat assistant](#chat-assistant)
- [Configuration](#configuration)
- [Extending MERRY](#extending-merry)
- [Troubleshooting](#troubleshooting)
- [Security notes](#security-notes)

---

## Architecture

```
            Streamlit sidebar: add files -> Submit
                              |
                              v
                 RAW_DB.PUBLIC.RAW_STAGE  (uploaded files)
                    |                         |
              CSV (ingest.load_csv)     PDF (ingest.extract_pdf)
                    |                    AI_CLASSIFY -> type
                    |                    AI_EXTRACT  -> JSON (1 call per file)
                    v                         v
      TIMESHEETS, DAILY_REPORTS,        DOC_EXTRACTS (raw JSON, one row per file)
      EQUIPMENT_LOG, GATE_LOG,                |
      RATE_CARD                               v
                    |                 typed views: TICKETS, TICKET_PERSONNEL, TICKET_EQUIPMENT,
                    |                 TICKET_QUANTITIES, INVOICES, INVOICE_LINES,
                    |                 CONTRACT_RATES, CONTRACT_TERMS, CONTRACT_TEXT
                    |                         |
                    +-----------+-------------+
                                v
         helper views: GATE_PERSON_HOURS, INVOICE_LINE_TICKETS, LINE_TICKET_SUPPORT, SITE_QUANTITIES
                                v
                    rule views R_* (one per rule)
                                v
              AUDIT_FLAGS (+ FLAG_REVIEW status)  ->  KPI_SUMMARY
                                v
                 Dashboard / Issues / Ask MERRY tabs
```

Key design choices:
- **AI reads, SQL decides.** AI is used only to read documents (and for chat). Every flag and amount is a SQL view.
- **Extract once.** Each PDF goes to AI once; the raw JSON is stored in `DOC_EXTRACTS`. Typed views over it
  can be changed and rebuilt without new AI calls.
- **Views, not copies.** All audit logic is in views, rebuilt by `audit_rules.create_views()` on every Submit.

---

## Ingestion

Triggered by the **Submit** button (`ingest.submit`):

1. `setup_db.setup_database()` creates the schema, file format and tables if missing (safe to repeat).
2. Selected files are uploaded to the stage (same name = overwrite).
3. Files to process = uploaded files **plus** any stage file not processed before.
   Files already processed and not re-uploaded are skipped (no repeated AI cost).
4. Each file is routed by extension:
   - **CSV** → `load_csv`: rows for that file are deleted and reloaded. Dates `M/D/YYYY` and `YYYY-MM-DD` both work.
   - **PDF** → `extract_pdf`:
     1. `classify_pdf` runs `AI_CLASSIFY` on the document with labels INVOICE / TICKET / CONTRACT / OTHER.
        If it errors, the file-name rule is used. OTHER is skipped.
     2. `AI_EXTRACT` runs with the format for that type (`EXTRACT_FORMATS` in `ingest.py`),
        `scale_factor` from `config.EXTRACT_SCALE_FACTOR` (2.0 fixed misread codes in testing).
     3. Contracts also store their full text (`AI_PARSE_DOCUMENT`, LAYOUT) for the chat assistant.
5. `audit_rules.create_views()` rebuilds all views; caches are cleared; the dashboard reloads.

### Fields extracted

| Type | Header fields | Tables |
|---|---|---|
| Ticket | ticket no., date, contractor, site, contract ref, approved by, remarks | Personnel (Name, Role, Hours), Equipment (ID, Description, Qty, Unit), Quantities (Item, Qty, Unit) |
| Invoice | invoice no., date, service period, contractor, contract ref, subtotal, VAT, total | Lines (Date, Ticket No., Description, Code, Qty, Unit, Rate, Amount) |
| Contract | agreement no., effective/expiry date, day-rate hours, standby %, travel cap | Exhibit A rates (Code, Item, Unit, Rate), Exhibit B schedules (Contractor, Code, Base, Term Start/End) |

### Formats handled

- **Dates**: `01/09/2026` (day-first), `09/14/2026` (month-first, detected per invoice), `01-Sep-2026`,
  `1 September 2026`, `01 Sep` (year from invoice), `2026-09-01`. Ambiguous ticket dates (`09/07/2026`)
  use the reading that matches the gate log / timesheets / equipment log for that contractor.
- **Ticket references** on invoice lines: single (`AQH-T-0409`), ranges (`PCS-T-7101 to PCS-T-7104`),
  lists (`T-1, T-3`). OCR variants like `AQH·T·0409` are normalized.
- **Named personnel lines**: `Crane Operator - P. Cruz` (checked person by person).
- **Lines without a rate code** (e.g. `Fuel surcharge`) are kept.

---

## CSV formats

All CSVs need a header row; columns are read by position in this order.

| File | Columns |
|---|---|
| `timesheets.csv` | work_date, worker_id, worker_name, contractor_code, role, hours, approved_by |
| `daily_reports.csv` | report_date, weather, ops_status (NORMAL / SUSPENDED), camp_headcount, activities, supervisor_notes, reported_by |
| `equipment_log.csv` | log_date, equipment_id, contractor_code, status (OPERATING / IDLE / OFF_HIRE / DEMOBILIZED), hours_run, remarks |
| `gate_log.csv` | gate_entry_id, entry_date, subject_type (PERSON / EQUIPMENT), subject_id, subject_name, contractor_code, gate, time_in, time_out (`YYYY-MM-DD HH:MI`), remarks |
| `rate_card.csv` | contractor_code, contractor_name, rate_code, item, unit, rate_php, standby_rate_php, term_start, term_end |

`daily_reports.activities` and `equipment_log.remarks` are also parsed for quantities, e.g.
`PumpCo pumping ops on PU-101, 27 t proppant`, `AquaHaul delivered 5 loads`, `Proppant pumped: 27 t`
(see `SITE_QUANTITIES`). The contractor is matched by the first word of its name in the contract (Exhibit B).

---

## Data model

All objects are in **`RAW_DB.AUDIT`**.

### Tables
| Table | Content |
|---|---|
| `TIMESHEETS`, `DAILY_REPORTS`, `EQUIPMENT_LOG`, `GATE_LOG`, `RATE_CARD` | CSV data + `source_file` |
| `DOC_EXTRACTS` | One row per PDF: `file_name`, `doc_type`, `extracted_at`, `result` (AI JSON) |
| `FLAG_REVIEW` | Reviewer decisions: `flag_id`, `status`, `note`, `updated_at` (latest row wins) |

### Views
| View | Content |
|---|---|
| `TICKETS`, `TICKET_PERSONNEL`, `TICKET_EQUIPMENT`, `TICKET_QUANTITIES` | Typed field-ticket data |
| `INVOICES`, `INVOICE_LINES` | Typed invoice header and lines (`ticket_ref` = ticket text as written) |
| `CONTRACT_RATES`, `CONTRACT_TERMS`, `CONTRACT_TEXT` | Exhibit A, Exhibit B, full MSA text |
| `GATE_PERSON_HOURS` | Hours on site per person per day (gate in → out) |
| `INVOICE_LINE_TICKETS` | One row per (invoice line, ticket) after expanding ranges; `work_date`, `billed_person` |
| `LINE_TICKET_SUPPORT` | Above + `supported_qty` = units that ticket supports (equipment days, crew per role, tonnes, loads) |
| `SITE_QUANTITIES` | Tonnes (`T`) / loads (`L`) per contractor per day from equipment log and daily reports |
| `R_*` | One view per audit rule (same columns) |
| `AUDIT_FLAGS` | Union of all rules + `flag_id`, `status`, `review_note` |
| `KPI_SUMMARY` | One row of dashboard totals |

`AUDIT_FLAGS` columns: `flag_id, rule, severity, contractor_code, source_doc, ticket_no, subject, work_date,
billed_value, actual_value, impact_php, reason, status, review_note`.
- `billed_value`: what was billed/recorded (pesos, hours, days or tonnes depending on the rule)
- `actual_value`: what the contract or site records allow
- `impact_php`: overbilled pesos (NULL when no rate applies)

---

## Audit rules

Defined in `audit_rules.py` → `RULES`. Contract references are to the MSA (PUP-MSA-2026-014).

| Rule (issue type) | Severity | Checks | Impact (PHP) | MSA |
|---|---|---|---|---|
| Ticket vs Timesheet Hours | MEDIUM / HIGH if gap ≥ 3 h | Ticket hours vs timesheet hours > `HOURS_TOLERANCE`; shows gate hours | hours × hourly rate, else ₱0 | 5.2–5.3 |
| Missing Timesheet | HIGH | Person on ticket, no timesheet that day | as above | 5.2 |
| Timesheet vs Gate Hours | MEDIUM | Timesheet > gate hours + `TRAVEL_CAP_HOURS` + tolerance | excess × hourly rate | 5.3–5.4 |
| No Gate Record | MEDIUM | Timesheet hours but no gate entry | excess × hourly rate | 3.2, 5.3 |
| Hours on Suspended Day | HIGH | Hours logged on a SUSPENDED day | day or hourly rate | 4.2 |
| Rate Above Contract | HIGH | Invoice rate > Exhibit A rate | difference × qty | 4.1 |
| Rate Above Rate Card | MEDIUM | Invoice rate > rate card CSV | difference × qty | – |
| Line Amount Error | HIGH | amount ≠ qty × rate | difference | 6.3 |
| Invoice Total Error | HIGH | lines ≠ subtotal, VAT ≠ 12%, total ≠ subtotal + VAT | difference | 6.3 |
| Invoice Line Without Ticket | HIGH | Referenced ticket not uploaded (per ticket in a range) | line share | 5.1, 6.2 |
| Qty vs Ticket | HIGH | Billed qty > what the tickets support | excess × rate | 5.1 |
| Date vs Ticket | MEDIUM | Single-ticket line date ≠ ticket date | – | 6.2 |
| Ticket vs Site Records | MEDIUM / HIGH | Ticket tonnes/loads ≠ site records (flagged even if not billed); invoice qty > site records | excess × rate when billed | 5.1–5.2, 7.1 |
| Loads vs Gate Entries | HIGH | Loads billed > trucks at the gate | excess × rate | 3.2 |
| Duplicate Ticket Billed | HIGH | Same ticket + rate code + person billed again | repeated amount | 6.1 |
| Role vs Ticket | HIGH | Named person billed in a different role than on the ticket | rate difference × qty | 4.1, 5.2 |
| Person Not on Ticket | HIGH | Named person not on the ticket | line amount | 5.1–5.2 |
| Billed Outside Contract Term | HIGH | Work date outside Exhibit B term | line share | 2.1 |
| Work Outside Contract Term | MEDIUM | Timesheet outside term | – | 2.1 |
| Billed on Suspended Day | HIGH | Billing on a SUSPENDED day: equipment allowed 50% standby, personnel 0 | billed − allowed | 4.2 |
| Non-Billable Item | HIGH | Fuel surcharge, mob/demob, meals (except camp services) | line amount | 7.3 |
| Ticket Not Approved | MEDIUM | No Company Representative approval | amounts billed on it | 1.2, 5.1 |

The plain-language explanation, contract basis and "what to check" for each rule are in
`dashboard.py` → `RULE_INFO` (shown in the dashboard table).

---

## Review workflow and KPIs

In the **Issues** tab select a flag → *Review status* → *Save review*. Each save adds a row to `FLAG_REVIEW`.

| Status | Meaning | Counted in |
|---|---|---|
| OPEN | Not reviewed (default) | Potential issues |
| CONFIRMED | Agreed overbilling, not yet recovered | Confirmed issues |
| RECOVERED | Closed – credit note received or short-paid | Confirmed issues, Recovered |
| DISMISSED | Closed – not an issue | – |

`flag_id` is an MD5 of rule + document + ticket + subject + date, so reviews survive view rebuilds.
If a document is re-extracted and those values change, the flag gets a new id (status resets to OPEN).

KPI cards (`KPI_SUMMARY`): Documents reviewed (PDFs + CSV files), Spend reviewed (sum of invoice totals incl. VAT),
Potential issues, Confirmed issues, Recovered.

---

## Chat assistant

`agent.py` → `ask()`:
1. The LLM (`config.AGENT_MODEL`) gets the column list of every AUDIT table/view plus `TABLE_NOTES`
   (join guidance, e.g. use `LINE_TICKET_SUPPORT` instead of `INVOICE_LINES.ticket_no` for ticket ranges)
   and writes one SELECT.
2. The SQL is rejected unless it is a single `SELECT`/`WITH` with no DML/DDL keywords; it is run wrapped in
   `SELECT * FROM (...) LIMIT 200`. On error it retries once with the error message.
3. The LLM answers from the query result + open flags + contract text.

The UI shows the SQL and rows under *How I got this*. `explain_flag()` writes a short explanation of one flag.

---

## Configuration

`config.py`:

| Setting | Default | Used for |
|---|---|---|
| `STAGE` | `@RAW_DB.PUBLIC.RAW_STAGE` | Uploaded files |
| `SCHEMA` | `RAW_DB.AUDIT` | All objects |
| `HOURS_TOLERANCE` | 1.0 | Allowed hours difference |
| `TRAVEL_CAP_HOURS` | 4.0 | MSA 5.4 travel allowance per day |
| `VAT_RATE` | 0.12 | Invoice total check |
| `AMOUNT_TOLERANCE` | 1.0 | Peso rounding in math checks |
| `EXTRACT_SCALE_FACTOR` | 2.0 | `AI_EXTRACT` OCR scale (1.0–4.0) |
| `AGENT_MODEL` | `claude-sonnet-4-5` | Chat assistant (`AI_COMPLETE`) |
| `CSV_TABLES` | 5 CSV types | CSV name → table |

After changing thresholds, click **Submit** (views are rebuilt).

---

## Extending MERRY

**Add an audit rule**
1. Add an entry to `RULES` in `audit_rules.py`. Its SELECT must return the 11 `FLAG_COLUMNS` in order:
   `rule, severity, contractor_code, source_doc, ticket_no, subject, work_date, billed_value, actual_value, impact_php, reason`.
2. Add the rule name to `RULE_INFO` in `dashboard.py` (why / contract basis / what to check).
3. Click **Submit**.

**Add a document type** (e.g. purchase orders)
1. Add a label to `DOC_TYPE_LABELS` and a matching key in `EXTRACT_FORMATS` (`ingest.py`).
2. Optionally add a file-name fallback in `pdf_type_for()`.
3. Add typed views in `audit_rules.py` → `TYPED_VIEWS`.

**Add a field to an existing type**
Add it to `EXTRACT_FORMATS`, expose it in the typed view, then re-upload those PDFs (they are re-extracted).

**Add a CSV type**
Add it to `CSV_TABLES` (`config.py`), `CSV_SELECT` (`ingest.py`) and a `CREATE TABLE` in `setup_db.py`.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Dashboard shows old numbers | Cached results (10 min) or app not restarted after code changes | Click **Submit** or **Run** |
| Many flags / impact larger than spend | Document read differently (date format, ticket ranges, layout) | Check the file in the **Data** tab; dismiss and report the pattern |
| Dates off by months | Ambiguous `MM/DD` vs `DD/MM` with no site record that day | Check `TICKETS.ticket_date`; add site records or adjust `TICKETS` view |
| PDF skipped as "not an invoice, ticket or contract" | `AI_CLASSIFY` chose OTHER | Check the document; adjust `DOC_TYPE_LABELS` descriptions |
| CSV skipped | File name doesn't start with a known prefix | Rename (see README) |
| Invoice Total Error on a correct invoice | A line was not extracted | Compare `INVOICE_LINES` with the PDF; re-upload |
| Chat says "no ticket found" | Model joined on `INVOICE_LINES.ticket_no` | Ask again mentioning tickets; check *How I got this* |

---

## Security notes

- No credentials are stored in the code; the app uses `st.connection("snowflake")` (Snowflake session identity).
- SQL built from user input uses bound parameters; table names in the Data tab come from a fixed allowlist.
- The chat assistant runs **LLM-generated** read-only SQL as the app owner's role. Run the app with a role
  that has only the grants listed in the README, not `ACCOUNTADMIN`.
