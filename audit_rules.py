"""Step 3 - Typed views over the PDFs, audit rules, and dashboard KPIs (all plain SQL views).

How it fits together:
    DOC_EXTRACTS (raw AI JSON)  ->  TICKETS / TICKET_PERSONNEL / INVOICES / INVOICE_LINES / CONTRACT_*
    typed tables + CSV tables   ->  one view per rule  (R_HOURS_MISMATCH, R_RATE_VS_CONTRACT, ...)
    all rule views              ->  AUDIT_FLAGS  ->  KPI_SUMMARY  ->  dashboard

Every rule view returns the same columns (see FLAG_COLUMNS) so they can be UNIONed.
To add a rule: add an entry to RULES and run create_views() again.
Views are re-created each time (CREATE OR REPLACE); no data is lost because views store none.
"""

from config import AMOUNT_TOLERANCE, HOURS_TOLERANCE, SCHEMA, TRAVEL_CAP_HOURS, VAT_RATE

S = SCHEMA


def _num(expr: str) -> str:
    """'22,500.00' -> 22500.00 (NULL when not a number)."""
    return f"TRY_TO_NUMBER(REPLACE({expr}::STRING, ',', ''), 14, 2)"


def _date(expr: str) -> str:
    """Parse the date formats seen in the PDFs: 01/09/2026, 1 January 2026, 2026-09-01."""
    e = f"{expr}::STRING"
    return (f"COALESCE(TRY_TO_DATE({e}, 'DD/MM/YYYY'), TRY_TO_DATE({e}, 'DD MMMM YYYY'), "
            f"TRY_TO_DATE({e}, 'DD Mon YYYY'), TRY_TO_DATE({e}, 'DD-Mon-YYYY'), TRY_TO_DATE({e}, 'YYYY-MM-DD'), "
            f"TRY_TO_DATE({e}, 'MM/DD/YYYY'))")


def _ticket(expr: str) -> str:
    """Normalize ticket numbers: 'AQH·T·0409' / 'aqh t 0409' -> 'AQH-T-0409'."""
    return f"UPPER(REGEXP_REPLACE(TRIM({expr}::STRING), '[^A-Za-z0-9]+', '-'))"


def _none(expr: str) -> str:
    """AI_EXTRACT returns the text 'None' when a field is missing."""
    return f"NULLIF(NULLIF(TRIM({expr}::STRING), 'None'), '')"


_SINGLE_TICKET = _ticket("NULLIF(piece, '')")   # used by INVOICE_LINE_TICKETS


# ============================================================================
# 1. Typed views over the PDF extraction JSON
# ============================================================================
TYPED_VIEWS = {
    "TICKETS": f"""
        -- '09/07/2026' is ambiguous (7 Sep or 9 Jul). Use the reading that matches the site records
        -- (gate log, timesheets, equipment log) for that contractor; default day-first.
        WITH raw AS (
            SELECT d.*, result:response:ticket_date::STRING AS date_text,
                   SPLIT_PART({_ticket('result:response:ticket_no')}, '-', 1) AS cc,
                   TRY_TO_DATE(result:response:ticket_date::STRING, 'DD/MM/YYYY') AS dm,
                   TRY_TO_DATE(result:response:ticket_date::STRING, 'MM/DD/YYYY') AS md
            FROM {S}.DOC_EXTRACTS d WHERE doc_type = 'TICKET'),
        ev AS (SELECT DISTINCT contractor_code, d FROM (
                   SELECT contractor_code, entry_date AS d FROM {S}.GATE_LOG
                   UNION ALL SELECT contractor_code, work_date FROM {S}.TIMESHEETS
                   UNION ALL SELECT contractor_code, log_date FROM {S}.EQUIPMENT_LOG))
        SELECT raw.file_name,
               {_ticket('raw.result:response:ticket_no')}                 AS ticket_no,
               CASE WHEN dm IS NOT NULL AND md IS NOT NULL AND dm <> md
                    THEN CASE WHEN e1.d IS NOT NULL THEN dm WHEN e2.d IS NOT NULL THEN md ELSE dm END
                    ELSE {_date('date_text')} END                         AS ticket_date,
               cc                                                         AS contractor_code,
               {_none('raw.result:response:contractor')}                  AS contractor_name,
               {_none('raw.result:response:site')}                        AS site,
               {_none('raw.result:response:contract_ref')}                AS contract_ref,
               {_none('raw.result:response:approved_by')}                 AS approved_by,
               {_none('raw.result:response:remarks')}                     AS remarks
        FROM raw
        LEFT JOIN ev e1 ON e1.contractor_code = raw.cc AND e1.d = raw.dm
        LEFT JOIN ev e2 ON e2.contractor_code = raw.cc AND e2.d = raw.md""",

    "TICKET_PERSONNEL": f"""
        SELECT t.file_name, t.ticket_no, t.ticket_date, t.contractor_code,
               TRIM(n.value::STRING)                                          AS worker_name,
               TRIM(d.result:response:personnel:Role[n.index]::STRING)        AS role,
               {_num('d.result:response:personnel:Hours[n.index]')}           AS hours
        FROM {S}.DOC_EXTRACTS d
        JOIN {S}.TICKETS t ON t.file_name = d.file_name,
             LATERAL FLATTEN(input => d.result:response:personnel:Name) n
        WHERE {_none('n.value')} IS NOT NULL""",

    "TICKET_EQUIPMENT": f"""
        SELECT t.file_name, t.ticket_no, t.ticket_date, t.contractor_code,
               TRIM(e.value::STRING)                                                AS equipment_id,
               TRIM(d.result:response:equipment:Description[e.index]::STRING)       AS description,
               {_num('d.result:response:equipment:Qty[e.index]')}                   AS qty,
               TRIM(d.result:response:equipment:Unit[e.index]::STRING)              AS unit
        FROM {S}.DOC_EXTRACTS d
        JOIN {S}.TICKETS t ON t.file_name = d.file_name,
             LATERAL FLATTEN(input => d.result:response:equipment:"Equipment ID") e
        WHERE {_none('e.value')} IS NOT NULL""",

    "TICKET_QUANTITIES": f"""
        SELECT t.file_name, t.ticket_no, t.ticket_date, t.contractor_code,
               TRIM(q.value::STRING)                                      AS item,
               {_num('d.result:response:quantities:Qty[q.index]')}        AS qty,
               TRIM(d.result:response:quantities:Unit[q.index]::STRING)   AS unit
        FROM {S}.DOC_EXTRACTS d
        JOIN {S}.TICKETS t ON t.file_name = d.file_name,
             LATERAL FLATTEN(input => d.result:response:quantities:Item) q
        WHERE {_none('q.value')} IS NOT NULL""",

    "INVOICES": f"""
        -- Detect day-first vs month-first per document: '09/14/2026' can only be month-first,
        -- so every date on that invoice is read month-first. Otherwise day-first (PH default).
        WITH raw AS (
            SELECT d.*, result:response:invoice_date::STRING AS di, result:response:period_start::STRING AS ds,
                   result:response:period_end::STRING AS de
            FROM {S}.DOC_EXTRACTS d WHERE doc_type = 'INVOICE'),
        fmt AS (
            SELECT raw.*,
                   (TRY_TO_DATE(di, 'DD/MM/YYYY') IS NULL AND TRY_TO_DATE(di, 'MM/DD/YYYY') IS NOT NULL)
                OR (TRY_TO_DATE(ds, 'DD/MM/YYYY') IS NULL AND TRY_TO_DATE(ds, 'MM/DD/YYYY') IS NOT NULL)
                OR (TRY_TO_DATE(de, 'DD/MM/YYYY') IS NULL AND TRY_TO_DATE(de, 'MM/DD/YYYY') IS NOT NULL) AS month_first
            FROM raw)
        SELECT file_name,
               {_none('result:response:invoice_no')}      AS invoice_no,
               COALESCE(IFF(month_first, TRY_TO_DATE(di, 'MM/DD/YYYY'), NULL), {_date('di')}) AS invoice_date,
               COALESCE(IFF(month_first, TRY_TO_DATE(ds, 'MM/DD/YYYY'), NULL), {_date('ds')}) AS period_start,
               COALESCE(IFF(month_first, TRY_TO_DATE(de, 'MM/DD/YYYY'), NULL), {_date('de')}) AS period_end,
               {_none('result:response:contractor')}      AS contractor_name,
               {_none('result:response:contract_ref')}    AS contract_ref,
               {_num('result:response:subtotal')}         AS subtotal,
               {_num('result:response:vat')}              AS vat,
               {_num('result:response:total_due')}        AS total_due
        FROM fmt""",

    # Line dates look like '01 Sep' (no year) -> borrow the year from the invoice date.
    # Lines WITHOUT a rate code (e.g. 'Fuel surcharge') are kept - they are often the non-billable ones.
    "INVOICE_LINES": f"""
        WITH x AS (
            SELECT i.file_name, i.invoice_no, i.invoice_date, l.index + 1 AS line_no,
                   COALESCE({_date("d.result:response:lines:Date[l.index]::STRING || ' ' || YEAR(i.invoice_date)")},
                            {_date('d.result:response:lines:Date[l.index]')})                AS line_date,
                   {_ticket('d.result:response:lines:"Ticket No."[l.index]')}                AS ticket_no,
                   {_none('d.result:response:lines:"Ticket No."[l.index]')}                  AS ticket_ref,
                   {_none('d.result:response:lines:Description[l.index]')}                   AS description,
                   UPPER({_none('d.result:response:lines:Code[l.index]')})                   AS rate_code,
                   {_num('d.result:response:lines:Qty[l.index]')}                           AS qty,
                   {_none('d.result:response:lines:Unit[l.index]')}                         AS unit,
                   {_num('d.result:response:lines:Rate[l.index]')}                          AS rate,
                   {_num('d.result:response:lines:"Amount (PHP)"[l.index]')}                AS amount
            FROM {S}.DOC_EXTRACTS d
            JOIN {S}.INVOICES i ON i.file_name = d.file_name,
                 LATERAL FLATTEN(input => d.result:response:lines:Description) l)
        SELECT file_name, invoice_no, invoice_date, line_no, line_date, ticket_no, ticket_ref, description,
               rate_code,
               -- code-less lines take the contractor of the other lines on the same invoice
               COALESCE(SPLIT_PART(rate_code, '-', 1),
                        MAX(SPLIT_PART(rate_code, '-', 1)) OVER (PARTITION BY file_name))   AS contractor_code,
               qty, unit, rate, amount
        FROM x
        WHERE description IS NOT NULL OR amount IS NOT NULL""",

    "CONTRACT_RATES": f"""
        SELECT d.file_name,
               UPPER(TRIM(r.value::STRING))                              AS rate_code,
               SPLIT_PART(UPPER(TRIM(r.value::STRING)), '-', 1)          AS contractor_code,
               TRIM(d.result:response:rates:Item[r.index]::STRING)       AS item,
               TRIM(d.result:response:rates:Unit[r.index]::STRING)       AS unit,
               {_num('d.result:response:rates:"Rate (PHP)"[r.index]')}   AS rate_php
        FROM {S}.DOC_EXTRACTS d, LATERAL FLATTEN(input => d.result:response:rates:Code) r
        WHERE d.doc_type = 'CONTRACT'""",

    "CONTRACT_TERMS": f"""
        SELECT d.file_name,
               TRIM(d.result:response:schedules:Contractor[c.index]::STRING)        AS contractor_name,
               UPPER(TRIM(c.value::STRING))                                         AS contractor_code,
               TRIM(d.result:response:schedules:Base[c.index]::STRING)              AS base,
               {_date('d.result:response:schedules:"Term Start"[c.index]')}         AS term_start,
               {_date('d.result:response:schedules:"Term End"[c.index]')}           AS term_end
        FROM {S}.DOC_EXTRACTS d, LATERAL FLATTEN(input => d.result:response:schedules:Code) c
        WHERE d.doc_type = 'CONTRACT'""",

    "CONTRACT_TEXT": f"""
        SELECT file_name, {_none('result:response:agreement_no')} AS agreement_no, result:text::STRING AS text
        FROM {S}.DOC_EXTRACTS WHERE doc_type = 'CONTRACT'""",

    # Helper: hours on site per person per day, from gate entry to gate exit (MSA 5.3)
    "GATE_PERSON_HOURS": f"""
        SELECT entry_date, subject_id, UPPER(TRIM(subject_name)) AS name_key, contractor_code,
               ROUND(SUM(DATEDIFF('minute', time_in, time_out)) / 60, 2) AS gate_hours
        FROM {S}.GATE_LOG WHERE subject_type = 'PERSON'
        GROUP BY 1, 2, 3, 4""",

    # One row per (invoice line, ticket). Expands ranges like 'PCS-T-7101 to PCS-T-7104' and lists
    # like 'T-1, T-3', so a summary line (no line date) is linked to each ticket and its date.
    "INVOICE_LINE_TICKETS": f"""
        WITH pieces AS (
            SELECT il.*, TRIM(p.value::STRING) AS piece,
                   REGEXP_REPLACE(TRIM(p.value::STRING), '\\\\s+(to|TO|To|through|thru)\\\\s+', '|') AS rng
            FROM {S}.INVOICE_LINES il,
                 LATERAL FLATTEN(input => SPLIT(COALESCE(il.ticket_ref, ''), ','), outer => TRUE) p),
        expanded AS (
            SELECT pieces.*,
                   CASE WHEN n.value::INT > 0 THEN   -- a range was expanded
                       {_ticket("REGEXP_REPLACE(SPLIT_PART(rng, '|', 1), '[0-9]+$', '') || LPAD(n.value::STRING, LENGTH(REGEXP_SUBSTR(SPLIT_PART(rng, '|', 1), '[0-9]+$')), '0')")}
                   ELSE {_SINGLE_TICKET} END AS one_ticket
            FROM pieces,
                 LATERAL FLATTEN(input => IFF(CONTAINS(rng, '|')
                        AND REGEXP_SUBSTR(SPLIT_PART(rng, '|', 1), '[0-9]+$') IS NOT NULL
                        AND REGEXP_SUBSTR(SPLIT_PART(rng, '|', 2), '[0-9]+$') IS NOT NULL,
                     ARRAY_GENERATE_RANGE(REGEXP_SUBSTR(SPLIT_PART(rng, '|', 1), '[0-9]+$')::INT,
                         LEAST(REGEXP_SUBSTR(SPLIT_PART(rng, '|', 2), '[0-9]+$')::INT,
                               REGEXP_SUBSTR(SPLIT_PART(rng, '|', 1), '[0-9]+$')::INT + 100) + 1),
                     [0]), outer => TRUE) n)
        SELECT e.file_name, e.invoice_no, e.invoice_date, e.line_no, e.line_date, e.ticket_ref,
               e.one_ticket AS ticket_no, t.ticket_date, t.ticket_no IS NOT NULL AS ticket_found,
               COALESCE(t.ticket_date, e.line_date) AS work_date,
               COUNT(*) OVER (PARTITION BY e.file_name, e.line_no) AS n_tickets,
               e.rate_code, e.contractor_code, COALESCE(cr.item, e.description) AS item, e.description,
               -- person named on the line, e.g. 'Crane Operator - P. Cruz' -> 'P. Cruz' (NULL if none)
               NULLIF(TRIM(REGEXP_SUBSTR(e.description, '-\\\\s*([A-Z][A-Za-z]*\\\\.?\\\\s*[A-Za-z]+)\\\\s*$', 1, 1, 'e', 1)), '')
                   AS billed_person,
               e.qty, e.unit, e.rate, e.amount
        FROM expanded e
        LEFT JOIN {S}.TICKETS t ON t.ticket_no = e.one_ticket
        LEFT JOIN {S}.CONTRACT_RATES cr ON cr.rate_code = e.rate_code""",

    # How many units each ticket supports for each invoice line:
    #   equipment day rates -> equipment listed on the ticket; personnel day/hour rates -> people with that
    #   role on the ticket (count or hours); per-load / per-tonne items -> ticket quantities with that unit.
    "LINE_TICKET_SUPPORT": f"""
        WITH eq AS (SELECT ticket_no, equipment_id, SUM(qty) AS q FROM {S}.TICKET_EQUIPMENT GROUP BY 1, 2),
             pe AS (SELECT ticket_no, UPPER(role) AS role, COUNT(*) AS n, SUM(hours) AS h
                    FROM {S}.TICKET_PERSONNEL GROUP BY 1, 2),
             qt AS (SELECT ticket_no, LEFT(UPPER(unit), 1) AS u, SUM(qty) AS q FROM {S}.TICKET_QUANTITIES
                    WHERE NOT (unit ILIKE 'day%' OR unit ILIKE 'h%') GROUP BY 1, 2)
        SELECT lt.*,
               CASE
                 WHEN NOT lt.ticket_found THEN NULL
                 WHEN lt.unit ILIKE 'day%' AND lt.rate_code LIKE '%-EQ-%' THEN COALESCE(SUM(eq.q), 0)
                 WHEN lt.unit ILIKE 'day%' THEN COALESCE(MAX(pe.n), 0)
                 WHEN lt.unit ILIKE 'h%' THEN COALESCE(MAX(pe.h), 0)
                 ELSE COALESCE(MAX(qt.q), 0)
               END AS supported_qty
        FROM {S}.INVOICE_LINE_TICKETS lt
        LEFT JOIN eq ON eq.ticket_no = lt.ticket_no AND lt.rate_code LIKE '%-EQ-%'
                    AND lt.item ILIKE '%' || eq.equipment_id || '%'
        LEFT JOIN pe ON pe.ticket_no = lt.ticket_no AND pe.role = UPPER(lt.item)
        LEFT JOIN qt ON qt.ticket_no = lt.ticket_no AND qt.u = LEFT(UPPER(lt.unit), 1)
        GROUP BY ALL""",

    # Quantities recorded on site, independent of the contractor's tickets:
    #   equipment log remarks ('Proppant pumped: 27 t') and daily report sentences that name the
    #   contractor ('PumpCo ... 27 t proppant', 'AquaHaul delivered 5 loads'). unit_key: T = tonnes, L = loads.
    "SITE_QUANTITIES": f"""
        WITH names AS (SELECT contractor_code, UPPER(SPLIT_PART(contractor_name, ' ', 1)) AS kw
                       FROM {S}.CONTRACT_TERMS),
             dr AS (SELECT d.report_date AS work_date, n.contractor_code, TRIM(s.value::STRING) AS sentence
                    FROM {S}.DAILY_REPORTS d, LATERAL SPLIT_TO_TABLE(COALESCE(d.activities, ''), '.') s, names n
                    WHERE UPPER(s.value::STRING) LIKE '%' || n.kw || '%'),
             site AS (
                SELECT work_date, contractor_code, 'T' AS unit_key,
                       TRY_TO_NUMBER(REGEXP_SUBSTR(sentence, '([0-9]+(\\\\.[0-9]+)?) ?t\\\\b', 1, 1, 'e', 1), 10, 2) AS qty,
                       'daily report' AS source FROM dr
                UNION ALL
                SELECT work_date, contractor_code, 'L',
                       TRY_TO_NUMBER(REGEXP_SUBSTR(sentence, '([0-9]+) loads?', 1, 1, 'ie', 1)), 'daily report' FROM dr
                UNION ALL
                SELECT log_date, contractor_code, 'T',
                       TRY_TO_NUMBER(REGEXP_SUBSTR(remarks, '([0-9]+(\\\\.[0-9]+)?) ?t\\\\b', 1, 1, 'e', 1), 10, 2),
                       'equipment log' FROM {S}.EQUIPMENT_LOG)
        SELECT work_date, contractor_code, unit_key,
               -- equipment log wins when both exist; keep both values for the reason text
               COALESCE(MAX(IFF(source = 'equipment log', qty, NULL)), MAX(qty)) AS site_qty,
               LISTAGG(source || ' ' || qty, ', ') WITHIN GROUP (ORDER BY source) AS site_detail
        FROM site WHERE qty IS NOT NULL
        GROUP BY 1, 2, 3""",
}

# ============================================================================
# 2. Audit rules - each returns FLAG_COLUMNS
# ============================================================================
FLAG_COLUMNS = ("rule, severity, contractor_code, source_doc, ticket_no, subject, work_date, "
                "billed_value, actual_value, impact_php, reason")

RULES = {
    # Ticket hours vs timesheet hours (catches Benjie Santos 10 h billed vs 5 h on 01-Sep)
    "R_HOURS_MISMATCH": f"""
        SELECT IFF(ts.hours IS NULL, 'Missing Timesheet', 'Ticket vs Timesheet Hours') AS rule,
               -- HIGH when the gap is large (e.g. Benjie 10 h vs 5 h), even if it has no peso value
               IFF(ts.hours IS NULL OR ABS(tp.hours - ts.hours) >= 2 * {HOURS_TOLERANCE} + 1, 'HIGH', 'MEDIUM') AS severity,
               tp.contractor_code, tp.file_name AS source_doc, tp.ticket_no,
               tp.worker_name AS subject, tp.ticket_date AS work_date,
               tp.hours AS billed_value, ts.hours AS actual_value,
               IFF(cr.unit = 'hour', GREATEST(tp.hours - ts.hours, 0) * cr.rate_php, NULL) AS impact_php,
               IFF(ts.hours IS NULL,
                   'Ticket bills ' || tp.hours || ' h but no timesheet row exists for this person/date',
                   'Ticket bills ' || tp.hours || ' h, timesheet shows ' || ts.hours || ' h'
                   || COALESCE(', gate log shows ' || g.gate_hours || ' h on site', '')) AS reason
        FROM {S}.TICKET_PERSONNEL tp
        LEFT JOIN {S}.TIMESHEETS ts
               ON UPPER(TRIM(ts.worker_name)) = UPPER(tp.worker_name) AND ts.work_date = tp.ticket_date
        LEFT JOIN {S}.GATE_PERSON_HOURS g
               ON g.name_key = UPPER(tp.worker_name) AND g.entry_date = tp.ticket_date
        LEFT JOIN {S}.CONTRACT_RATES cr
               ON cr.contractor_code = tp.contractor_code AND UPPER(cr.item) = UPPER(tp.role)
        WHERE ts.hours IS NULL OR ABS(tp.hours - ts.hours) > {HOURS_TOLERANCE}""",

    # Timesheet hours vs gate entry/exit, allowing contract travel time (MSA 5.3 / 5.4)
    "R_GATE_HOURS": f"""
        SELECT IFF(g.gate_hours IS NULL, 'No Gate Record', 'Timesheet vs Gate Hours') AS rule,
               'MEDIUM', ts.contractor_code, ts.source_file, NULL, ts.worker_name, ts.work_date,
               ts.hours, g.gate_hours,
               IFF(cr.unit = 'hour',
                   GREATEST(ts.hours - COALESCE(g.gate_hours, 0) - {TRAVEL_CAP_HOURS}, 0) * cr.rate_php, NULL),
               IFF(g.gate_hours IS NULL,
                   'Timesheet shows ' || ts.hours || ' h but person has no gate entry that day',
                   'Timesheet ' || ts.hours || ' h exceeds gate time ' || g.gate_hours
                   || ' h + ' || {TRAVEL_CAP_HOURS} || ' h travel cap')
        FROM {S}.TIMESHEETS ts
        LEFT JOIN {S}.GATE_PERSON_HOURS g ON g.subject_id = ts.worker_id AND g.entry_date = ts.work_date
        LEFT JOIN {S}.CONTRACT_RATES cr
               ON cr.contractor_code = ts.contractor_code AND UPPER(cr.item) = UPPER(ts.role)
        WHERE ts.hours > 0
          AND (g.gate_hours IS NULL OR ts.hours > g.gate_hours + {TRAVEL_CAP_HOURS} + {HOURS_TOLERANCE})""",

    # Personnel stood down on suspended days are not billable (MSA 4.2)
    "R_HOURS_ON_SUSPENDED_DAY": f"""
        SELECT 'Hours on Suspended Day', 'HIGH', ts.contractor_code, ts.source_file, NULL,
               ts.worker_name, ts.work_date, ts.hours, 0,
               CASE cr.unit WHEN 'hour' THEN ts.hours * cr.rate_php WHEN 'day' THEN cr.rate_php END,
               'Daily report marks ' || ts.work_date || ' SUSPENDED (' || dr.weather || ') but '
               || ts.hours || ' h were logged'
        FROM {S}.TIMESHEETS ts
        JOIN {S}.DAILY_REPORTS dr ON dr.report_date = ts.work_date AND dr.ops_status = 'SUSPENDED'
        LEFT JOIN {S}.CONTRACT_RATES cr
               ON cr.contractor_code = ts.contractor_code AND UPPER(cr.item) = UPPER(ts.role)
        WHERE ts.hours > 0""",

    # Invoice rate higher than the contract rate (Exhibit A)
    "R_RATE_VS_CONTRACT": f"""
        SELECT 'Rate Above Contract', 'HIGH', il.contractor_code, il.file_name, il.ticket_no,
               il.rate_code, il.line_date, il.rate, cr.rate_php,
               (il.rate - cr.rate_php) * il.qty,
               'Invoice rate ' || il.rate || ' vs contract rate ' || cr.rate_php || ' for ' || il.rate_code
        FROM {S}.INVOICE_LINES il
        JOIN {S}.CONTRACT_RATES cr ON cr.rate_code = il.rate_code
        WHERE il.rate > cr.rate_php + {AMOUNT_TOLERANCE}""",

    # Invoice rate higher than the rate card CSV (the rate card may disagree with the contract)
    "R_RATE_VS_RATE_CARD": f"""
        SELECT 'Rate Above Rate Card', 'MEDIUM', il.contractor_code, il.file_name, il.ticket_no,
               il.rate_code, il.line_date, il.rate, rc.rate_php,
               (il.rate - rc.rate_php) * il.qty,
               'Invoice rate ' || il.rate || ' vs rate card ' || rc.rate_php
               || COALESCE(' (contract says ' || cr.rate_php || ' - confirm which rate applies)', '')
        FROM {S}.INVOICE_LINES il
        JOIN {S}.RATE_CARD rc ON UPPER(rc.rate_code) = il.rate_code
             AND COALESCE(il.line_date, il.invoice_date) BETWEEN rc.term_start AND rc.term_end
        LEFT JOIN {S}.CONTRACT_RATES cr ON cr.rate_code = il.rate_code
        WHERE il.rate > rc.rate_php + {AMOUNT_TOLERANCE}""",

    # Line amount must equal qty x rate
    "R_LINE_MATH": f"""
        SELECT 'Line Amount Error', 'HIGH', contractor_code, file_name, ticket_no, rate_code, line_date,
               amount, qty * rate, amount - qty * rate,
               'Line ' || line_no || ': ' || qty || ' x ' || rate || ' = ' || (qty * rate) || ', billed ' || amount
        FROM {S}.INVOICE_LINES
        WHERE ABS(amount - qty * rate) > {AMOUNT_TOLERANCE}""",

    # Invoice header math: subtotal, VAT, total
    "R_INVOICE_TOTALS": f"""
        WITH x AS (
            SELECT i.*, SUM(il.amount) AS lines_sum
            FROM {S}.INVOICES i LEFT JOIN {S}.INVOICE_LINES il ON il.file_name = i.file_name
            GROUP BY ALL)
        SELECT 'Invoice Total Error', 'HIGH', NULL, file_name, NULL, invoice_no, invoice_date,
               total_due, ROUND(lines_sum * (1 + {VAT_RATE}), 2),
               total_due - ROUND(lines_sum * (1 + {VAT_RATE}), 2),
               'Lines sum ' || lines_sum || ', subtotal ' || subtotal || ', VAT ' || vat
               || ', total ' || total_due || ' (expected ' || ROUND(lines_sum * (1 + {VAT_RATE}), 2) || ')'
        FROM x
        WHERE ABS(subtotal - lines_sum) > {AMOUNT_TOLERANCE}
           OR ABS(vat - ROUND(subtotal * {VAT_RATE}, 2)) > {AMOUNT_TOLERANCE}
           OR ABS(total_due - (subtotal + vat)) > {AMOUNT_TOLERANCE}""",

    # Charges not supported by a field ticket are not payable (MSA 5.1)
    # One flag per missing ticket; a line covering several tickets is split evenly between them.
    "R_NO_TICKET": f"""
        SELECT 'Invoice Line Without Ticket', 'HIGH', contractor_code, file_name, ticket_no,
               rate_code, work_date, amount / n_tickets, 0, amount / n_tickets,
               'Line ' || line_no || ' (' || COALESCE(ticket_ref, 'no ticket reference') || '): ticket '
               || COALESCE(ticket_no, '(blank)') || ' not found among uploaded field tickets'
        FROM {S}.INVOICE_LINE_TICKETS
        WHERE NOT ticket_found
          -- fuel / mob-demob / meals are handled (and counted once) by R_NOT_BILLABLE
          AND NOT (ticket_ref IS NULL AND description ILIKE ANY ('%fuel%', '%mobiliz%', '%demob%', '%meal%'))""",

    # Invoiced quantity vs what the tickets support (days of equipment/people, loads, tonnes...)
    # plus invoice date vs ticket date for single-ticket lines.
    "R_TICKET_MISMATCH": f"""
        WITH s AS (
            SELECT file_name, invoice_no, line_no, ANY_VALUE(contractor_code) AS contractor_code,
                   ANY_VALUE(ticket_ref) AS ticket_ref, ANY_VALUE(rate_code) AS rate_code,
                   ANY_VALUE(item) AS item, ANY_VALUE(unit) AS unit, ANY_VALUE(qty) AS qty,
                   ANY_VALUE(rate) AS rate, MIN(work_date) AS work_date, SUM(supported_qty) AS supported
            -- lines that name a person are checked one by one in R_ROLE_VS_TICKET instead
            FROM {S}.LINE_TICKET_SUPPORT WHERE ticket_found AND billed_person IS NULL GROUP BY 1, 2, 3)
        SELECT 'Qty vs Ticket', 'HIGH', contractor_code, file_name, ticket_ref, rate_code, work_date,
               qty, supported, (qty - supported) * rate,
               'Line ' || line_no || ' bills ' || qty || ' ' || unit || ' of ' || item
               || ', tickets ' || ticket_ref || ' support ' || supported
        FROM s WHERE qty > supported + 0.01
        UNION ALL
        SELECT 'Date vs Ticket', 'MEDIUM', contractor_code, file_name, ticket_no, rate_code, line_date,
               NULL, NULL, NULL,
               'Invoice line dated ' || line_date || ', ticket dated ' || ticket_date
        FROM {S}.INVOICE_LINE_TICKETS
        WHERE ticket_found AND n_tickets = 1 AND line_date IS NOT NULL AND line_date <> ticket_date""",

    # Ticket tonnes/loads vs independent site records (equipment log + daily report) - MSA 5.2, 7.1
    "R_TICKET_VS_SITE": f"""
        -- (a) every ticket whose tonnes/loads differ from site records - flagged even if never billed
        WITH tq AS (SELECT t.ticket_no, t.file_name, t.ticket_date, t.contractor_code,
                           LEFT(UPPER(q.unit), 1) AS unit_key, SUM(q.qty) AS ticket_qty, ANY_VALUE(q.item) AS item
                    FROM {S}.TICKETS t JOIN {S}.TICKET_QUANTITIES q ON q.ticket_no = t.ticket_no
                    WHERE LEFT(UPPER(q.unit), 1) IN ('T', 'L')
                    GROUP BY 1, 2, 3, 4, 5),
        -- (b) invoice lines: billed qty vs site records summed over the line's ticket days
             billed AS (SELECT s.file_name, s.invoice_no, s.line_no, s.contractor_code, s.rate_code, s.ticket_ref,
                               s.item, s.unit, LEFT(UPPER(s.unit), 1) AS unit_key, s.qty, s.rate,
                               MIN(s.work_date) AS work_date, SUM(sq.site_qty) AS site_total,
                               -- ticket days with a billable quantity but no site record -> can't compare
                               COUNT_IF(sq.site_qty IS NULL AND COALESCE(s.supported_qty, 1) > 0) AS missing_days
                        FROM {S}.LINE_TICKET_SUPPORT s
                        LEFT JOIN {S}.SITE_QUANTITIES sq ON sq.work_date = s.work_date
                             AND sq.contractor_code = s.contractor_code AND sq.unit_key = LEFT(UPPER(s.unit), 1)
                        WHERE LEFT(UPPER(s.unit), 1) IN ('T', 'L')
                        GROUP BY ALL)
        SELECT 'Ticket vs Site Records', 'MEDIUM', tq.contractor_code, tq.file_name, tq.ticket_no, tq.item,
               tq.ticket_date, tq.ticket_qty, sq.site_qty, NULL,
               'Ticket shows ' || tq.ticket_qty || IFF(tq.unit_key = 'T', ' t', ' loads') || ' but site records show '
               || sq.site_qty || ' (' || sq.site_detail || ') - data discrepancy even if not billed'
        FROM tq JOIN {S}.SITE_QUANTITIES sq
          ON sq.work_date = tq.ticket_date AND sq.contractor_code = tq.contractor_code AND sq.unit_key = tq.unit_key
        WHERE ABS(tq.ticket_qty - sq.site_qty) > 0.01
        UNION ALL
        SELECT 'Ticket vs Site Records', 'HIGH', contractor_code, file_name, ticket_ref, rate_code, work_date,
               qty, site_total, (qty - site_total) * rate,
               'Line ' || line_no || ' bills ' || qty || ' ' || unit || ' of ' || item
               || ', site records for those days show ' || site_total || ' - overbilled'
        FROM billed WHERE site_total IS NOT NULL AND missing_days = 0 AND qty > site_total + 0.01""",

    # Per-load billing vs trucks recorded at the gate that day
    "R_LOADS_VS_GATE": f"""
        WITH g AS (SELECT entry_date, contractor_code, COUNT(*) AS entries
                   FROM {S}.GATE_LOG WHERE subject_type = 'EQUIPMENT' GROUP BY 1, 2)
        SELECT 'Loads vs Gate Entries', 'HIGH', il.contractor_code, il.file_name, il.ticket_no,
               il.rate_code, il.line_date, il.qty, COALESCE(g.entries, 0),
               GREATEST(il.qty - COALESCE(g.entries, 0), 0) * il.rate,
               'Invoiced ' || il.qty || ' loads, gate log shows ' || COALESCE(g.entries, 0) || ' truck entries'
        FROM {S}.INVOICE_LINES il
        LEFT JOIN g ON g.entry_date = il.line_date AND g.contractor_code = il.contractor_code
        WHERE il.unit ILIKE 'load%' AND il.qty > COALESCE(g.entries, 0)""",

    # A field ticket may be billed only once per rate code and person (MSA 6.1).
    # Different codes on one ticket are normal (pump + crew), and so are two named people with the same
    # role (two crane operators); the same ticket + code + person twice is not.
    "R_DUPLICATE_TICKET": f"""
        SELECT 'Duplicate Ticket Billed', 'HIGH', contractor_code, file_name, ticket_no,
               rate_code || COALESCE(' - ' || billed_person, ''), work_date,
               COALESCE(supported_qty * rate, amount / n_tickets), 0, COALESCE(supported_qty * rate, amount / n_tickets),
               'Ticket ' || ticket_no || ' (' || rate_code || COALESCE(', ' || billed_person, '')
               || ') billed again - first billed on '
               || FIRST_VALUE(invoice_no) OVER (PARTITION BY ticket_no, rate_code, UPPER(COALESCE(billed_person, ''))
                                                ORDER BY invoice_date, invoice_no, line_no)
               || ' line '
               || FIRST_VALUE(line_no) OVER (PARTITION BY ticket_no, rate_code, UPPER(COALESCE(billed_person, ''))
                                             ORDER BY invoice_date, invoice_no, line_no)
        FROM {S}.LINE_TICKET_SUPPORT
        WHERE ticket_found
        QUALIFY ROW_NUMBER() OVER (PARTITION BY ticket_no, rate_code, UPPER(COALESCE(billed_person, ''))
                                   ORDER BY invoice_date, invoice_no, line_no) > 1""",

    # Named personnel lines ('Crane Operator - P. Cruz'): the person must be on the ticket, in the role
    # billed. A Rigger billed as a Crane Operator is overbilled by the rate difference (MSA 4.1, 5.2).
    "R_ROLE_VS_TICKET": f"""
        WITH lp AS (
            SELECT lt.*, tp.worker_name, tp.role AS ticket_role, cr.rate_php AS role_rate
            FROM {S}.INVOICE_LINE_TICKETS lt
            LEFT JOIN {S}.TICKET_PERSONNEL tp
                   ON tp.ticket_no = lt.ticket_no
                  AND UPPER(SPLIT_PART(tp.worker_name, ' ', -1)) = UPPER(SPLIT_PART(lt.billed_person, ' ', -1))
            LEFT JOIN {S}.CONTRACT_RATES cr
                   ON cr.contractor_code = lt.contractor_code AND UPPER(cr.item) = UPPER(tp.role)
            WHERE lt.ticket_found AND lt.billed_person IS NOT NULL)
        SELECT IFF(worker_name IS NULL, 'Person Not on Ticket', 'Role vs Ticket'), 'HIGH',
               contractor_code, file_name, ticket_no, COALESCE(worker_name, billed_person), work_date,
               rate, IFF(worker_name IS NULL, 0, role_rate),
               IFF(worker_name IS NULL, amount, GREATEST(rate - role_rate, 0) * qty),
               IFF(worker_name IS NULL,
                   billed_person || ' billed as ' || item || ' but is not on ticket ' || ticket_no,
                   worker_name || ' billed as ' || item || ' at ' || rate || ' but ticket ' || ticket_no
                   || ' lists them as ' || ticket_role || COALESCE(' (contract rate ' || role_rate || ')', ''))
        FROM lp
        WHERE worker_name IS NULL OR UPPER(ticket_role) <> UPPER(item)""",

    # Work outside the contractor's term is not payable (MSA 2.1, Exhibit B)
    "R_OUT_OF_TERM": f"""
        SELECT 'Billed Outside Contract Term', 'HIGH', lt.contractor_code, lt.file_name, lt.ticket_no,
               lt.rate_code, lt.work_date, lt.amount / lt.n_tickets, 0, lt.amount / lt.n_tickets,
               'Work dated ' || lt.work_date || ', term is ' || ct.term_start || ' to ' || ct.term_end
        FROM {S}.INVOICE_LINE_TICKETS lt
        JOIN {S}.CONTRACT_TERMS ct ON ct.contractor_code = lt.contractor_code
        WHERE lt.work_date NOT BETWEEN ct.term_start AND ct.term_end
        UNION ALL
        SELECT 'Work Outside Contract Term', 'MEDIUM', ts.contractor_code, ts.source_file, NULL,
               ts.worker_name, ts.work_date, ts.hours, 0, NULL,
               'Timesheet dated ' || ts.work_date || ', term is ' || ct.term_start || ' to ' || ct.term_end
        FROM {S}.TIMESHEETS ts
        JOIN {S}.CONTRACT_TERMS ct ON ct.contractor_code = ts.contractor_code
        WHERE ts.hours > 0 AND ts.work_date NOT BETWEEN ct.term_start AND ct.term_end""",

    # Equipment idle due to weather is billable at 50% only; other work is not billable (MSA 4.2).
    # Checked per ticket day, using only units that ticket supports (so a summary line is split correctly).
    "R_BILLED_ON_SUSPENDED_DAY": f"""
        WITH x AS (
            SELECT s.*, dr.weather, COALESCE(s.supported_qty, s.qty) * s.rate AS billed_php,
                   s.rate_code LIKE '%-EQ-%' AS is_equipment
            FROM {S}.LINE_TICKET_SUPPORT s
            JOIN {S}.DAILY_REPORTS dr ON dr.report_date = s.work_date AND dr.ops_status = 'SUSPENDED'
            WHERE COALESCE(s.supported_qty, s.qty) > 0)
        SELECT 'Billed on Suspended Day', 'HIGH', contractor_code, file_name, ticket_no, rate_code, work_date,
               billed_php, IFF(is_equipment, billed_php * 0.5, 0),
               billed_php - IFF(is_equipment, billed_php * 0.5, 0),
               'Daily report marks ' || work_date || ' SUSPENDED (' || weather || '); ' || item
               || IFF(is_equipment, ' billed at full day rate - idle equipment is billable at 50% standby',
                      ' billed - personnel stood down are not billable')
        FROM x""",

    # Not billable: fuel surcharges, mob/demob, meals for camp-housed crew (MSA 7.3)
    "R_NOT_BILLABLE": f"""
        SELECT 'Non-Billable Item', 'HIGH', contractor_code, file_name, ticket_no, rate_code, line_date,
               amount, 0, amount, 'MSA 7.3 excludes: ' || description
        FROM {S}.INVOICE_LINES
        WHERE description ILIKE ANY ('%fuel%', '%mobiliz%', '%demob%', '%meal%')
          AND COALESCE(rate_code, '') NOT LIKE 'KCS-%'""",

    # Field ticket not approved by the Company Representative
    "R_MISSING_APPROVAL": f"""
        SELECT 'Ticket Not Approved', 'MEDIUM', t.contractor_code, t.file_name, t.ticket_no, NULL,
               t.ticket_date, NULL, NULL, SUM(COALESCE(s.supported_qty * s.rate, s.amount / s.n_tickets)),
               'No Company Representative approval on ticket'
        FROM {S}.TICKETS t LEFT JOIN {S}.LINE_TICKET_SUPPORT s ON s.ticket_no = t.ticket_no
        WHERE t.approved_by IS NULL
        GROUP BY ALL""",
}

# ============================================================================
# 3. Combined flags + KPIs for the dashboard
# ============================================================================
def _flags_sql() -> str:
    union = "\n        UNION ALL\n".join(
        f"        SELECT {FLAG_COLUMNS} FROM {S}.{name}" for name in RULES)
    return f"""
        WITH f AS (
{union}
        ),
        review AS (
            SELECT * FROM {S}.FLAG_REVIEW
            QUALIFY ROW_NUMBER() OVER (PARTITION BY flag_id ORDER BY updated_at DESC) = 1)
        SELECT MD5(CONCAT_WS('|', f.rule, f.source_doc, COALESCE(f.ticket_no, ''),
                             COALESCE(f.subject, ''), COALESCE(f.work_date::STRING, ''))) AS flag_id,
               f.*, COALESCE(r.status, 'OPEN') AS status, r.note AS review_note
        FROM f
        LEFT JOIN review r
          ON r.flag_id = MD5(CONCAT_WS('|', f.rule, f.source_doc, COALESCE(f.ticket_no, ''),
                                       COALESCE(f.subject, ''), COALESCE(f.work_date::STRING, '')))"""


SUMMARY_VIEWS = {
    "AUDIT_FLAGS": _flags_sql(),
    "KPI_SUMMARY": f"""
        SELECT
            (SELECT COUNT(*) FROM {S}.DOC_EXTRACTS)
          + (SELECT COUNT(DISTINCT source_file) FROM (
                SELECT source_file FROM {S}.TIMESHEETS UNION SELECT source_file FROM {S}.DAILY_REPORTS
                UNION SELECT source_file FROM {S}.EQUIPMENT_LOG UNION SELECT source_file FROM {S}.GATE_LOG
                UNION SELECT source_file FROM {S}.RATE_CARD))                       AS docs_reviewed,
            (SELECT COALESCE(SUM(total_due), 0) FROM {S}.INVOICES)                AS spend_reviewed,
            COALESCE(SUM(IFF(status = 'OPEN', impact_php, 0)), 0)                AS potential_issues,
            COALESCE(SUM(IFF(status IN ('CONFIRMED', 'RECOVERED'), impact_php, 0)), 0) AS confirmed_issues,
            COALESCE(SUM(IFF(status = 'RECOVERED', impact_php, 0)), 0)          AS recovered,
            COUNT_IF(status = 'OPEN')                                              AS open_flags,
            COUNT(*)                                                               AS total_flags
        FROM {S}.AUDIT_FLAGS WHERE status <> 'DISMISSED'""",
}


def create_views(session) -> None:
    """(Re)create typed views, rule views, AUDIT_FLAGS and KPI_SUMMARY - in dependency order."""
    for group in (TYPED_VIEWS, RULES, SUMMARY_VIEWS):
        for name, sql in group.items():
            # Rule views get the shared column list so their SELECTs can stay unaliased.
            cols = f" ({FLAG_COLUMNS})" if group is RULES else ""
            session.sql(f"CREATE OR REPLACE VIEW {S}.{name}{cols} AS {sql}").collect()


def set_flag_status(session, flag_id: str, status: str, note: str = "") -> None:
    """Record a reviewer decision. Status: OPEN | CONFIRMED | RECOVERED | DISMISSED."""
    if status not in {"OPEN", "CONFIRMED", "RECOVERED", "DISMISSED"}:
        raise ValueError(f"Invalid status {status}")
    session.sql(
        f"INSERT INTO {S}.FLAG_REVIEW VALUES (?, ?, ?, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ)",
        params=[flag_id, status, note],
    ).collect()
