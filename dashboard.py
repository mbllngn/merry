"""Step 5 - Dashboard widgets. Display only: every function receives data that is already loaded."""

import pandas as pd
import streamlit as st

STATUSES = ["OPEN", "CONFIRMED", "RECOVERED", "DISMISSED"]

# Plain-language explanation for every rule name produced in audit_rules.py:
# rule -> (why it is a potential issue, contract basis, what the reviewer should check)
RULE_INFO = {
    "Ticket vs Timesheet Hours": ("Hours on the field ticket differ from the hours on the timesheet",
                                  "MSA 5.2-5.3", "Compare ticket, timesheet and gate log; ask contractor which is correct"),
    "Missing Timesheet": ("Person billed on a ticket has no timesheet for that day",
                          "MSA 5.2", "Request the timesheet or remove the billed hours"),
    "Timesheet vs Gate Hours": ("Timesheet hours exceed time on site (gate in to gate out) plus allowed travel",
                                "MSA 5.3-5.4", "Check gate log times and travel claims"),
    "No Gate Record": ("Timesheet shows hours but the person never passed the gate that day",
                       "MSA 3.2, 5.3", "Confirm the person was on site"),
    "Hours on Suspended Day": ("Hours logged on a day the daily report marks operations SUSPENDED",
                               "MSA 4.2", "Personnel stood down are not billable - remove unless on site by instruction"),
    "Rate Above Contract": ("Invoice rate is higher than the Exhibit A contract rate",
                            "MSA 4.1, Exhibit A", "Short-pay the difference"),
    "Rate Above Rate Card": ("Invoice rate is higher than the rate card CSV",
                             "Rate card vs MSA Exhibit A", "Confirm which rate is agreed (rate card may be an amendment)"),
    "Line Amount Error": ("Line amount is not qty x rate", "MSA 6.3 (correct invoice)", "Recalculate the line"),
    "Invoice Total Error": ("Subtotal, VAT or total does not add up", "MSA 6.3 (correct invoice)",
                            "Ask for a corrected invoice"),
    "Invoice Line Without Ticket": ("Invoice line has no matching signed field ticket",
                                    "MSA 5.1, 6.2", "Not payable until the ticket is provided"),
    "Qty vs Ticket": ("Invoiced quantity differs from the field ticket", "MSA 5.1", "Pay the ticket quantity"),
    "Date vs Ticket": ("Invoice line date differs from the field ticket date", "MSA 6.2",
                       "Check for mis-dated or re-used tickets"),
    "Loads vs Gate Entries": ("More loads invoiced than trucks recorded at the gate", "MSA 3.2",
                              "Pay only loads supported by gate entries"),
    "Duplicate Ticket Billed": ("Same field ticket billed more than once", "MSA 6.1", "Reject the repeat line"),
    "Billed Outside Contract Term": ("Work billed outside the contractor's term", "MSA 2.1, Exhibit B",
                                     "Not payable unless a written extension exists (MSA 2.3)"),
    "Work Outside Contract Term": ("Timesheet hours outside the contractor's term", "MSA 2.1, Exhibit B",
                                   "Check for an extension before any billing"),
    "Billed on Suspended Day": ("Work billed on a day operations were SUSPENDED", "MSA 4.2",
                                "Idle equipment at 50% standby only; personnel not billable"),
    "Non-Billable Item": ("Fuel surcharge, mob/demob or camp meals billed", "MSA 7.3",
                          "Remove unless pre-approved in writing"),
    "Ticket Not Approved": ("Field ticket has no Company Representative approval", "MSA 1.2, 5.1",
                            "Get the ticket signed before paying"),
    "Ticket vs Site Records": ("Ticket or invoice tonnes/loads differ from the equipment log or daily report "
                               "(flagged even if not billed; peso impact when the invoice bills more)",
                               "MSA 5.1-5.2, 7.1",
                               "Correct the ticket; pay only the quantity in the site records"),
    "Role vs Ticket": ("Person billed in a higher-rate role than the ticket shows (e.g. Rigger billed as Crane "
                       "Operator)", "MSA 4.1, 5.2", "Pay the contract rate for the role on the ticket"),
    "Person Not on Ticket": ("Person named on the invoice is not on the field ticket", "MSA 5.1-5.2",
                             "Not payable unless a signed ticket shows them"),
}


def _php(value) -> str:
    return f"₱{float(value or 0):,.2f}"


def show_kpis(kpi: pd.DataFrame, flags: pd.DataFrame | None = None) -> None:
    """Top row of metric cards, from AUDIT.KPI_SUMMARY."""
    if kpi.empty:
        st.info("No KPI data yet - click **Submit** in the sidebar.")
        return
    k = kpi.iloc[0]
    c = st.columns(5)
    c[0].metric("📄 Documents reviewed", int(k["DOCS_REVIEWED"]))
    c[1].metric("💰 Spend reviewed", _php(k["SPEND_REVIEWED"]), help="Sum of invoice TOTAL DUE (incl. VAT)")
    c[2].metric("⚠️ Potential issues", _php(k["POTENTIAL_ISSUES"]),
                delta=f"{int(k['OPEN_FLAGS'])} open flags", delta_color="off",
                help="Peso value of open flags. Flags with no peso value (e.g. hours mismatches) are still counted in open flags")
    c[3].metric("✅ Confirmed issues", _php(k["CONFIRMED_ISSUES"]), help="Flags a reviewer marked CONFIRMED or RECOVERED")
    c[4].metric("🔄 Recovered", _php(k["RECOVERED"]), help="Flags a reviewer marked RECOVERED")


def show_charts(flags: pd.DataFrame) -> None:
    """Impact and flag counts by rule and contractor."""
    active = flags[flags["STATUS"] != "DISMISSED"]
    if active.empty:
        st.success("No issues found 🎉")
        return
    left, right = st.columns(2)
    with left:
        st.markdown("**Flags by issue type**")
        st.bar_chart(active.groupby("RULE")["FLAG_ID"].count().sort_values(ascending=False), horizontal=True)
    with right:
        st.markdown("**Flags by contractor**")
        st.bar_chart(active.groupby("CONTRACTOR_CODE", dropna=False)["FLAG_ID"].count())


def show_issue_breakdown(flags: pd.DataFrame) -> None:
    """Why is there a potential issue? One row per issue type, then the flags behind each one.

    Uses OPEN flags only, so the total matches the 'Potential issues' card.
    """
    open_flags = flags[flags["STATUS"] == "OPEN"].copy()
    for col in ("IMPACT_PHP", "BILLED_VALUE", "ACTUAL_VALUE"):   # Snowflake NUMBER -> Decimal; make it float
        open_flags[col] = pd.to_numeric(open_flags[col], errors="coerce")
    st.subheader("🔎 Why there are potential issues")
    if open_flags.empty:
        st.success("No open issues.")
        return

    total = float(open_flags["IMPACT_PHP"].fillna(0).sum())
    summary = (open_flags.groupby("RULE")
               .agg(Severity=("SEVERITY", "first"), Flags=("FLAG_ID", "count"),
                    Contractors=("CONTRACTOR_CODE", lambda s: ", ".join(sorted(s.dropna().unique()))),
                    Documents=("SOURCE_DOC", "nunique"), Impact=("IMPACT_PHP", lambda s: s.fillna(0).sum()))
               .reset_index().sort_values(["Impact", "Flags"], ascending=False))
    summary["Share"] = summary["Impact"] / total * 100 if total else 0.0
    summary["Why it is an issue"] = summary["RULE"].map(lambda r: RULE_INFO.get(r, ("", "", ""))[0])
    summary["Contract basis"] = summary["RULE"].map(lambda r: RULE_INFO.get(r, ("", "", ""))[1])
    summary["What to check"] = summary["RULE"].map(lambda r: RULE_INFO.get(r, ("", "", ""))[2])

    st.dataframe(
        summary, hide_index=True, width="stretch",
        column_order=["RULE", "Severity", "Flags", "Contractors", "Documents", "Impact", "Share",
                      "Why it is an issue", "Contract basis", "What to check"],
        column_config={
            "RULE": st.column_config.TextColumn("Issue type"),
            "Impact": st.column_config.NumberColumn("Overbilled / at risk (PHP)", format="₱%.2f",
                                                    help="Amount that can be rejected or recovered - not the amount billed"),
            "Share": st.column_config.ProgressColumn("% of potential", format="%.0f%%", min_value=0, max_value=100),
            "Why it is an issue": st.column_config.TextColumn(width="large"),
            "What to check": st.column_config.TextColumn(width="large"),
        },
    )
    no_peso = int(open_flags["IMPACT_PHP"].isna().sum())
    if no_peso:
        st.caption(f"{no_peso} flag(s) have no peso impact yet (e.g. hours mismatches on per-load billing) - "
                   "they still need review but add ₱0 to the total.")

    # Detail: the individual flags behind each issue type
    for rule in summary["RULE"]:
        rows = open_flags[open_flags["RULE"] == rule]
        impact = float(rows["IMPACT_PHP"].fillna(0).sum())
        with st.expander(f"{rule} - {len(rows)} flag(s), {_php(impact)}"):
            st.dataframe(
                rows[["CONTRACTOR_CODE", "SOURCE_DOC", "TICKET_NO", "SUBJECT", "WORK_DATE",
                      "BILLED_VALUE", "ACTUAL_VALUE", "IMPACT_PHP", "REASON"]],
                hide_index=True, width="stretch",
                column_config={
                    "CONTRACTOR_CODE": st.column_config.TextColumn("Contractor"),
                    "SOURCE_DOC": st.column_config.TextColumn("Document"),
                    "TICKET_NO": st.column_config.TextColumn("Ticket"),
                    "SUBJECT": st.column_config.TextColumn("Person / item"),
                    "WORK_DATE": st.column_config.DateColumn("Date"),
                    "BILLED_VALUE": st.column_config.NumberColumn(
                        "Billed", help="What the contractor billed or recorded (PHP, hours, days or tonnes)"),
                    "ACTUAL_VALUE": st.column_config.NumberColumn(
                        "Allowed / actual", help="What the contract or site records support"),
                    "IMPACT_PHP": st.column_config.NumberColumn(
                        "Overbilled (PHP)", format="₱%.2f", help="Billed minus allowed, in pesos"),
                    "REASON": st.column_config.TextColumn("Reason", width="large"),
                },
            )


def filter_flags(flags: pd.DataFrame) -> pd.DataFrame:
    """Filter widgets (applied in pandas, no SQL built from user input)."""
    c1, c2, c3 = st.columns(3)
    contractors = c1.multiselect("Contractor", sorted(flags["CONTRACTOR_CODE"].dropna().unique()))
    rules = c2.multiselect("Issue type", sorted(flags["RULE"].unique()))
    statuses = c3.multiselect("Status", STATUSES, default=["OPEN", "CONFIRMED"])
    out = flags
    if contractors:
        out = out[out["CONTRACTOR_CODE"].isin(contractors)]
    if rules:
        out = out[out["RULE"].isin(rules)]
    if statuses:
        out = out[out["STATUS"].isin(statuses)]
    return out


def show_flag_table(flags: pd.DataFrame):
    """Issue breakdown table. Returns the selected flag row (or None)."""
    cols = ["RULE", "SEVERITY", "CONTRACTOR_CODE", "SOURCE_DOC", "TICKET_NO", "SUBJECT", "WORK_DATE",
            "BILLED_VALUE", "ACTUAL_VALUE", "IMPACT_PHP", "STATUS", "REASON"]
    event = st.dataframe(
        flags[cols], hide_index=True, width="stretch",
        on_select="rerun", selection_mode="single-row",
        column_config={
            "BILLED_VALUE": st.column_config.NumberColumn("Billed"),
            "ACTUAL_VALUE": st.column_config.NumberColumn("Allowed / actual"),
            "IMPACT_PHP": st.column_config.NumberColumn("Overbilled (PHP)", format="₱%.2f"),
            "WORK_DATE": st.column_config.DateColumn("Date"),
            "REASON": st.column_config.TextColumn("Reason", width="large"),
        },
    )
    rows = event.selection.rows
    return flags.iloc[rows[0]] if rows else None
