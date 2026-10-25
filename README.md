# MERRY – Contractor Invoice Audit

MERRY is a Streamlit in Snowflake app that audits contractor invoices against field tickets,
the contract (MSA), and independent site records (timesheets, gate log, equipment log, daily reports).

It reads PDFs with Snowflake Cortex AI (`AI_CLASSIFY`, `AI_EXTRACT`), loads CSVs into typed tables,
and runs **SQL audit rules**, so every flagged peso amount comes from SQL, not from an LLM.

| What it does | How |
|---|---|
| Ingest invoices, field tickets, contract (PDF) | `AI_CLASSIFY` detects the type, `AI_EXTRACT` reads all fields in one call per file |
| Ingest timesheets, daily reports, equipment log, gate log, rate card (CSV) | Loaded directly into typed tables |
| Find overbilling and data discrepancies | 20+ SQL rules (rates, quantities, roles, hours, standby, non-billable items, duplicates…) |
| Dashboard | KPIs, "why there are potential issues" breakdown, charts |
| Review workflow | Mark each flag OPEN / CONFIRMED / RECOVERED / DISMISSED |
| Chat assistant | Ask questions; it writes read-only SQL over the audit data and shows its work |

See **[DOCUMENTATION.md](DOCUMENTATION.md)** for the data model, every audit rule, and how to extend the app.

---

## Prerequisites

### Snowflake account
- A Snowflake account with **Streamlit in Snowflake – Workspaces (container runtime)** available.
- **Cortex AI functions** available in your region (directly or via cross-region inference):
  `AI_EXTRACT`, `AI_CLASSIFY`, `AI_PARSE_DOCUMENT`, `AI_COMPLETE`.
  - `AI_CLASSIFY` on **documents** is in preview and region-limited. If it is unavailable, MERRY falls back
    to detecting the document type from the file name (see [File naming](#file-naming)).
- Cross-region inference enabled if your region does not host the models natively:
  ```sql
  ALTER ACCOUNT SET CORTEX_ENABLED_CROSS_REGION = 'ANY_REGION';
  ```

### Role and privileges
The role that owns/runs the app needs:

| Privilege | On |
|---|---|
| `USAGE` | warehouse (default `COMPUTE_WH`) and compute pool (default `SYSTEM_COMPUTE_POOL_CPU`) |
| `USAGE`, `CREATE SCHEMA` | database `RAW_DB` |
| `READ`, `WRITE` | stage `RAW_DB.PUBLIC.RAW_STAGE` |
| `CREATE TABLE`, `CREATE VIEW`, `CREATE FILE FORMAT` | schema `RAW_DB.AUDIT` (created by the app) |
| Database role `SNOWFLAKE.CORTEX_USER` | for the AI functions |

> The app was developed with `ACCOUNTADMIN`. For production use a dedicated role with only the grants above.

### Stage
Create the database, schema and stage once (directory table and server-side encryption are required for AI functions):

```sql
CREATE DATABASE IF NOT EXISTS RAW_DB;
CREATE SCHEMA IF NOT EXISTS RAW_DB.PUBLIC;
CREATE STAGE IF NOT EXISTS RAW_DB.PUBLIC.RAW_STAGE
  DIRECTORY = (ENABLE = TRUE)
  ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE');
```

### Python
- Python **3.11** (Streamlit in Workspaces runtime). No extra packages: `pyproject.toml` uses the pre-installed `streamlit[snowflake]`.

---

## Setup

1. **Create the stage** (see above).
2. **Add the app to a Snowflake Workspace**: create a workspace from this Git repository
   (Snowsight → Projects → Workspaces → *From Git repository*), or upload the files to any workspace folder.
3. **Check `snowflake.yml`**: set `query_warehouse` and `compute_pool` to resources your role can use.
4. Open `streamlit_app.py` and click **Run**.
5. In the sidebar, add your files and click **Submit**. On first run, Submit also creates the `RAW_DB.AUDIT`
   schema, tables and views, and processes any files already on the stage.

To change settings (stage, schema, tolerances, VAT, AI model), edit **`config.py`**.

---

## File naming

The PDF type is detected from the **content** by `AI_CLASSIFY`. File names matter only for CSVs
(and as a fallback for PDFs):

| File | Name must start with / contain | Loaded into |
|---|---|---|
| Timesheets CSV | `timesheets…` | `TIMESHEETS` |
| Daily reports CSV | `daily…` | `DAILY_REPORTS` |
| Equipment log CSV | `equipment…` | `EQUIPMENT_LOG` |
| Gate log CSV | `gate…` | `GATE_LOG` |
| Rate card CSV | `rate…` | `RATE_CARD` |
| Invoice PDF (fallback) | `-INV-` or `invoice` | `INVOICES`, `INVOICE_LINES` |
| Field ticket PDF (fallback) | `-T-` or `ticket` | `TICKETS`, `TICKET_*` |
| Contract PDF (fallback) | `contract` or `msa` | `CONTRACT_*` |

CSVs must have a header row. Expected columns are listed in [DOCUMENTATION.md](DOCUMENTATION.md#csv-formats).

---

## Using the app

| Tab | Purpose |
|---|---|
| 📊 **Dashboard** | KPI cards, "why there are potential issues" table (per issue type, with contract clause and what to check), charts |
| ⚠️ **Issues** | All flags; filter by contractor/type/status; select a row to set its review status or have the AI explain it |
| 💬 **Ask MERRY** | Chat over all data; expand *How I got this* to see the SQL and rows behind each answer |
| 📂 **Data** | Browse the loaded tables |

**KPI cards**
- **Potential issues**: peso value of OPEN flags
- **Confirmed issues**: flags marked CONFIRMED or RECOVERED
- **Recovered**: flags marked RECOVERED (closed, money credited or short-paid)

---

## Project structure

```
streamlit_app.py      Page layout, sidebar Submit button, tabs
config.py             Settings: stage, schema, thresholds, VAT, AI model
setup_db.py           Creates RAW_DB.AUDIT schema, file format, tables
ingest.py             Upload, AI_CLASSIFY + AI_EXTRACT for PDFs, CSV loading
audit_rules.py        SQL views: typed PDF data, site quantities, audit rules, AUDIT_FLAGS, KPI_SUMMARY
agent.py              Chat assistant (read-only SQL generation + answer)
dashboard.py          Dashboard widgets and issue-type explanations
snowflake.yml         Streamlit app definition (warehouse, compute pool, artifacts)
pyproject.toml        Python dependencies (default runtime)
DOCUMENTATION.md      Detailed documentation
```

---

## Limitations

- The rules were built and tested on one project's documents (4 contractors, September 2026).
  New contractors or document layouts may need rule refinements; review the first batch from each new source.
- Peso impact is only computed where a rate applies (e.g. hours mismatches on per-load billing show ₱0).
- The chat assistant's answers depend on LLM-generated SQL; always check *How I got this*.
- Data and Snowflake objects are not stored in this repository; they live in your Snowflake account.
