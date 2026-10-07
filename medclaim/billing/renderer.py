# billing/renderer.py
# Statement text renderer. Extracted from build_statement() (see ADR note in
# statement.py). Pure presentation: no DB calls, no side effects.

from datetime import datetime


def render_statement_text(
    patient_id,
    payer_id,
    contract,
    as_of,
    allowed_rate,
    working,
    total_charges,
    total_adjustments,
    total_payments,
    estimated_patient_due,
):
    """Produce the human-readable statement text.

    working dicts must already have patient_due and expected_allowed set
    (done by the per-line pass in build_statement before this is called).
    Returns the rendered text string.
    """
    balance = total_charges - total_adjustments - total_payments

    # ------------------------------------------- aging + delinquency
    aging = {"0_30": 0, "31_60": 0, "61_90": 0, "over_90": 0}
    today = datetime.utcnow().date()
    for w in working:
        if w["paid"] > 0:
            continue
        try:
            y, m, d = w["service_date"].split("-")
            sdt = datetime(int(y), int(m), int(d)).date()
            age_days = (today - sdt).days
        except Exception:
            # malformed date on an old row; treat as current and move on
            age_days = 0
        owed = w.get("patient_due", 0)
        if age_days <= 30:
            aging["0_30"] = aging["0_30"] + owed
        elif age_days <= 60:
            aging["31_60"] = aging["31_60"] + owed
        elif age_days <= 90:
            aging["61_90"] = aging["61_90"] + owed
        else:
            aging["over_90"] = aging["over_90"] + owed
    delinquent = aging["over_90"] > 0

    # ------------------------------------------- payer status summary
    status_counts = {}
    for w in working:
        st = w["status"] or "submitted"
        status_counts[st] = status_counts.get(st, 0) + 1

    # ------------------------------------------- render (text)
    by_month = {}
    for w in working:
        by_month.setdefault(w["month"], []).append(w)

    text_out = []
    text_out.append("=" * 44)
    text_out.append("PATIENT STATEMENT")
    # NOTE: patient name intentionally NOT rendered into logs here; the text
    # is returned to the caller, not logged.
    text_out.append("Account: %s" % patient_id)
    text_out.append("Payer:   %s" % payer_id)
    if contract is not None and contract.effective_date is not None:
        text_out.append("Contract as of %s: %d%% allowed" % (as_of, allowed_rate))
    text_out.append("=" * 44)

    for month in sorted(by_month.keys()):
        text_out.append("")
        text_out.append("Service month %s" % month)
        text_out.append("-" * 44)
        month_charge = 0
        for w in by_month[month]:
            # columns are load-bearing for the old print pipeline; keep widths
            flag = ""
            if w["status"] and w["status"] != "submitted":
                flag = " [%s]" % w["status"]
            text_out.append("  %-14s %8d%s" % (w["id"], w["charge"], flag))
            if w["adjustment"]:
                text_out.append("      adjustment        -%8d" % w["adjustment"])
            if w["paid"]:
                text_out.append("      payer paid        -%8d" % w["paid"])
            elif w.get("patient_due"):
                text_out.append("      you may owe (est) %9d" % w["patient_due"])
            month_charge = month_charge + w["charge"]
        text_out.append("  %-14s %8d" % ("month charges", month_charge))

    text_out.append("")
    text_out.append("=" * 44)
    text_out.append("Total charges     %10d" % total_charges)
    text_out.append("Total adjustments %10d" % total_adjustments)
    text_out.append("Payments received %10d" % total_payments)
    text_out.append("Balance due       %10d" % balance)
    if estimated_patient_due:
        text_out.append("Estimated to owe  %10d" % estimated_patient_due)
    if delinquent:
        text_out.append("** account past due: balance over 90 days **")
    if aging["over_90"] or aging["61_90"]:
        text_out.append("Aging  0-30 %d  31-60 %d  61-90 %d  90+ %d"
                        % (aging["0_30"], aging["31_60"], aging["61_90"], aging["over_90"]))
    text_out.append("Claims on statement: %d" % sum(status_counts.values()))
    text_out.append("=" * 44)

    # ------------------------------------------- remittance detail
    paid_rows = [w for w in working if w["paid"] > 0]
    if paid_rows:
        text_out.append("")
        text_out.append("Payments received")
        text_out.append("-" * 44)
        for w in sorted(paid_rows, key=lambda x: x["service_date"]):
            covered = 0
            if w["charge"]:
                covered = (w["paid"] * 100) // w["charge"]
            text_out.append("  %-14s paid %8d  (%d%% of charge)"
                            % (w["id"], w["paid"], covered))
        text_out.append("  %-14s      %8d" % ("payments total", total_payments))

    # ------------------------------------------- summary by service month
    text_out.append("")
    text_out.append("Summary by service month")
    text_out.append("-" * 44)
    for month in sorted(by_month.keys()):
        mc = 0
        ma = 0
        for w in by_month[month]:
            mc = mc + w["charge"]
            ma = ma + w["adjustment"]
        text_out.append("  %-10s charges %8d  adjust %8d" % (month, mc, ma))

    # ------------------------------------------- patient messages
    messages = []
    if balance <= 0:
        messages.append("Your account is paid in full. Thank you.")
    else:
        messages.append("Please remit the balance due by the date on your bill.")
    if delinquent:
        messages.append("Part of your balance is more than 90 days old. "
                        "Please contact billing to avoid further action.")
    if estimated_patient_due and total_payments == 0:
        messages.append("Some claims are still with your insurer; amounts shown "
                        "as estimated may change once they are processed.")
    if any(w["adjustment"] for w in working):
        messages.append("Adjustments reflect discounts or corrections already "
                        "applied to your account.")
    if messages:
        text_out.append("")
        text_out.append("Messages")
        text_out.append("-" * 44)
        for msg in messages:
            # naive wrap at ~40 cols; old code, do not bother with textwrap
            line = ""
            for word in msg.split():
                if len(line) + len(word) + 1 > 40:
                    text_out.append("  " + line)
                    line = word
                else:
                    line = (line + " " + word).strip()
            if line:
                text_out.append("  " + line)

    text_out.append("")
    text_out.append("=" * 44)
    return "\n".join(text_out)
