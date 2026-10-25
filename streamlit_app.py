"""MERRY - invoice audit app.

Files in this app (read them in this order):
  config.py       settings (stage, schema, thresholds, model)
  setup_db.py     creates the AUDIT schema and tables
  ingest.py       loads CSVs and extracts PDFs (AI_EXTRACT) into those tables
  audit_rules.py  SQL views: typed PDF data, one view per audit rule, AUDIT_FLAGS, KPI_SUMMARY
  agent.py        chat assistant (writes read-only SQL over the AUDIT data, then answers)
  dashboard.py    dashboard widgets
  streamlit_app.py  this file: page layout and buttons
"""

import os

import pandas as pd
import streamlit as st

import agent
import audit_rules
import dashboard
import ingest
import setup_db
from config import SCHEMA

st.set_page_config(page_title="MERRY", page_icon="🧾", layout="wide")

conn = st.connection("snowflake", ttl=os.getenv("SNOWFLAKE_CONNECTION_TTL"))
session = conn.session()


# -------------------------------------------------------------------
# Cached loaders (cleared after ingestion or a review change)
# -------------------------------------------------------------------
@st.cache_data(ttl="10m", show_spinner=False)
def load_kpis():
    return session.sql(f"SELECT * FROM {SCHEMA}.KPI_SUMMARY").to_pandas()


@st.cache_data(ttl="10m", show_spinner=False)
def load_flags():
    return session.sql(
        f"""SELECT * FROM {SCHEMA}.AUDIT_FLAGS
            ORDER BY IFF(severity = 'HIGH', 0, 1), impact_php DESC NULLS LAST, work_date""").to_pandas()


@st.cache_data(ttl="1h", show_spinner=False)
def load_schema_description():
    return agent.schema_description(session)


# Tables the Data tab may show (fixed list - never build table names from user input)
DATA_TABLES = ["TIMESHEETS", "DAILY_REPORTS", "EQUIPMENT_LOG", "GATE_LOG", "RATE_CARD",
               "TICKETS", "TICKET_PERSONNEL", "TICKET_QUANTITIES", "INVOICES", "INVOICE_LINES",
               "CONTRACT_RATES", "CONTRACT_TERMS"]


@st.cache_data(ttl="10m", show_spinner=False)
def load_table(name: str):
    if name not in DATA_TABLES:
        raise ValueError(name)
    return session.sql(f"SELECT * FROM {SCHEMA}.{name} LIMIT 1000").to_pandas()


def clear_caches() -> None:
    load_kpis.clear()
    load_flags.clear()
    load_table.clear()
    load_schema_description.clear()


def audit_ready() -> bool:
    try:
        session.sql(f"SELECT 1 FROM {SCHEMA}.KPI_SUMMARY LIMIT 1").collect()
        return True
    except Exception:
        return False


# -------------------------------------------------------------------
# Sidebar: ingestion
# -------------------------------------------------------------------
with st.sidebar:
    st.header("📥 Data")

    # A new key after each submit gives an empty uploader (Streamlit has no "clear uploader" call).
    st.session_state.setdefault("uploader_key", 0)
    uploads = st.file_uploader("➕ Add files (CSV or PDF)", type=["pdf", "csv"], accept_multiple_files=True,
                               key=f"uploader_{st.session_state.uploader_key}",
                               help="Add all files first, then click Submit. Re-uploading a file replaces its old data.")

    # One button does everything: setup -> upload -> classify/extract/load -> rebuild audit -> reload dashboard
    if st.button("Submit", type="primary", width="stretch"):
        with st.status("Processing files...", expanded=True) as status:
            setup_db.setup_database(session)
            messages = ingest.submit(session, uploads, on_progress=st.write)
            st.write("Rebuilding audit checks...")
            audit_rules.create_views(session)
            status.update(label="Done", state="complete")
        clear_caches()
        st.session_state.last_ingest = messages or ["No new files - everything on the stage is already processed."]
        st.session_state.uploader_key += 1           # -> empty uploader
        st.rerun()                                   # -> dashboard reloads with the new data

    if st.session_state.get("last_ingest"):
        with st.expander(f"✅ Last submit ({len(st.session_state.last_ingest)} item(s))", expanded=True):
            for msg in st.session_state.last_ingest:
                st.write(msg)
            if st.button("Dismiss", key="dismiss_ingest"):
                st.session_state.last_ingest = []
                st.rerun()

# -------------------------------------------------------------------
# Main page
# -------------------------------------------------------------------
st.title("🧾 MERRY - Invoice Audit")

if not audit_ready():
    st.info("Audit data is not set up yet. Click **Submit** in the sidebar (with or without new files).")
    st.stop()

tab_dash, tab_issues, tab_chat, tab_data = st.tabs(["📊 Dashboard", "⚠️ Issues", "💬 Ask MERRY", "📂 Data"])

with st.spinner("Loading audit results..."):
    kpis, flags = load_kpis(), load_flags()

with tab_dash:
    dashboard.show_kpis(kpis, flags)
    st.divider()
    dashboard.show_issue_breakdown(flags)
    st.divider()
    dashboard.show_charts(flags)

with tab_issues:
    st.subheader("Issue breakdown")
    selected = dashboard.show_flag_table(dashboard.filter_flags(flags))
    if selected is not None:
        st.markdown(f"**{selected['RULE']}** · {selected['SOURCE_DOC']} · {selected['REASON']}")
        c1, c2 = st.columns([1, 2])
        with c1:
            new_status = st.selectbox("Review status", dashboard.STATUSES,
                                      index=dashboard.STATUSES.index(selected["STATUS"]))
            note = st.text_input("Note", value="" if pd.isna(selected["REVIEW_NOTE"]) else selected["REVIEW_NOTE"])
            if st.button("Save review"):
                audit_rules.set_flag_status(session, selected["FLAG_ID"], new_status, note)
                clear_caches()
                st.rerun()
        with c2:
            if st.button("🤖 Explain this issue"):
                with st.spinner("Thinking..."):
                    st.write(agent.explain_flag(session, selected.to_dict()))
    else:
        st.caption("Select a row to review or explain it.")

with tab_chat:
    st.subheader("Ask about any document, log or issue")
    if "messages" not in st.session_state:
        st.session_state.messages = []
    for m in st.session_state.messages:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
            if m.get("sql"):
                with st.expander("How I got this (SQL + data)"):
                    st.code(m["sql"], language="sql")
                    if m.get("data") is not None:
                        st.dataframe(m["data"], hide_index=True)

    if question := st.chat_input("e.g. Did Benjie Santos bill more hours than he worked?"):
        st.session_state.messages.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"):
            with st.spinner("Checking the data..."):
                result = agent.ask(session, question, load_schema_description())
            st.markdown(result["answer"])
            if result["sql"]:
                with st.expander("How I got this (SQL + data)"):
                    st.code(result["sql"], language="sql")
                    if result["data"] is not None:
                        st.dataframe(result["data"], hide_index=True)
                    if result["error"]:
                        st.caption(f"Query error: {result['error']}")
        st.session_state.messages.append(
            {"role": "assistant", "content": result["answer"], "sql": result["sql"], "data": result["data"]})

with tab_data:
    table = st.selectbox("Table", DATA_TABLES)
    st.dataframe(load_table(table), hide_index=True, width="stretch")
