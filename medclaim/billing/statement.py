# billing/statement.py
# ============================================================================
# ORIGINAL BUILD (2018). The statement engine. build_statement() does the lot:
# pulls the patient, pulls their claims, aggregates charges/adjustments/payments,
# resolves the payer contract, estimates allowed amounts, groups and RENDERS the
# statement text, and writes a statements row. One function. No tests. Every
# engineer who inherited it decided today was not the day.
#
# Money is in CENTS throughout. Some comments below say dollars. They are wrong
# and have been wrong for years -- trust the integers, not the comments.
# ============================================================================

import uuid
from datetime import datetime

from medclaim import db
# resolve_contract is imported into THIS module's namespace on purpose: statement
# code must go through the single resolver (ADR-003).
from medclaim.billing.contracts import resolve_contract
from medclaim.billing.renderer import render_statement_text
from medclaim.models import Statement, StatementLine


def aggregate_claims(conn, patient_id):
    """Query claims and remittances for a patient; return aggregated data.

    conn must be an open connection. Returns a dict with keys:
        working              list of claim dicts
        total_charges        int (cents)
        total_adjustments    int (cents)
        total_payments       int (cents)
        newest_service_date  str | None
    """
    # is_deleted is not 1  ->  show active (0) and legacy (NULL), hide the
    # modern-voided (1). see ADR-002.
    claim_rows = conn.execute(
        "select id, patient_id, payer_id, service_date, amount_cents, "
        "adjustment_cents, status, is_deleted "
        "from claims where patient_id = ? and is_deleted is not 1 "
        "order by service_date",
        (patient_id,),
    ).fetchall()

    total_charges = 0        # dollars  (<- lie: cents)
    total_adjustments = 0    # dollars  (<- lie: cents)
    total_payments = 0
    newest_service_date = None
    working = []
    for c in claim_rows:
        charge = c["amount_cents"]
        adjustment = c["adjustment_cents"] or 0

        pay_row = conn.execute(
            "select coalesce(sum(paid_cents), 0) as paid "
            "from remittances where claim_id = ?",
            (c["id"],),
        ).fetchone()
        paid = pay_row["paid"] if pay_row is not None else 0

        total_charges = total_charges + charge
        total_adjustments = total_adjustments + adjustment
        total_payments = total_payments + paid

        sd = c["service_date"]
        if newest_service_date is None or sd > newest_service_date:
            newest_service_date = sd

        # crude month bucket for the grouped render further down. era-1 string
        # slicing; service_date is ISO 'YYYY-MM-DD'.
        month_key = sd[0:7] if sd else "0000-00"

        working.append({
            "id": c["id"],
            "service_date": sd,
            "month": month_key,
            "charge": charge,
            "adjustment": adjustment,
            "paid": paid,
            "status": c["status"],
        })

    return {
        "working": working,
        "total_charges": total_charges,
        "total_adjustments": total_adjustments,
        "total_payments": total_payments,
        "newest_service_date": newest_service_date,
    }


def resolve_statement_rate(payer_id, newest_service_date):
    """Derive the contract and allowed rate for a statement.

    Computes as_of from newest_service_date (falls back to utcnow when None).
    Calls resolve_contract (ADR-003 single entry point).

    Returns (contract, allowed_rate, as_of).
    """
    if newest_service_date is not None:
        as_of = newest_service_date
    else:
        as_of = datetime.utcnow().date().isoformat()
    contract = resolve_contract(payer_id, as_of)
    if contract is not None:
        allowed_rate = contract.rate_pct
    else:
        allowed_rate = 0   # no contract on file: payer allows nothing
    return contract, allowed_rate, as_of


def build_statement(patient_id):
    conn = db.connect()
    try:
        # ----------------------------------------------------------- patient
        patient = conn.execute(
            "select patient_id, name, mrn, payer_id from patients where patient_id = ?",
            (patient_id,),
        ).fetchone()
        if patient is None:
            # era-1 behaviour: empty statement rather than raise, so the nightly
            # batch does not die on one bad id.
            conn.close()
            return Statement(patient_id=patient_id, total_charges_cents=0,
                             total_adjustments_cents=0, balance_cents=0, lines=[])
        payer_id = patient["payer_id"]

        # ---------------------------------------------------------- aggregate
        agg = aggregate_claims(conn, patient_id)

        # --------------------------------------------------------- contract
        contract, allowed_rate, as_of = resolve_statement_rate(
            payer_id, agg["newest_service_date"]
        )

        # ------------------------------------------------------- per-line pass
        # build display lines; for unpaid claims estimate the patient-owed part
        # using the resolved rate. the estimate is informational only.
        lines = []
        estimated_patient_due = 0
        for w in agg["working"]:
            charge = w["charge"]
            adjustment = w["adjustment"]
            paid = w["paid"]

            expected_allowed = (charge * allowed_rate) // 100   # cents
            w["expected_allowed"] = expected_allowed

            if paid > 0:
                w["patient_due"] = 0
            else:
                not_allowed = charge - expected_allowed
                due = not_allowed - adjustment
                if due < 0:
                    due = 0
                w["patient_due"] = due
                estimated_patient_due = estimated_patient_due + due

            lines.append(StatementLine(
                claim_id=w["id"],
                service_date=w["service_date"],
                charge_cents=charge,
                is_voided=False,   # voided rows were filtered out above
            ))

        # ------------------------------------------------------------ balance
        # what the patient owes: charges minus write-offs (adjustments) minus
        # what the payer has actually paid. the estimate above does NOT feed it.
        balance = (agg["total_charges"]
                   - agg["total_adjustments"]
                   - agg["total_payments"])

        # ------------------------------------------------------ render (text)
        statement_text = render_statement_text(
            patient_id=patient_id,
            payer_id=payer_id,
            contract=contract,
            as_of=as_of,
            allowed_rate=allowed_rate,
            working=agg["working"],
            total_charges=agg["total_charges"],
            total_adjustments=agg["total_adjustments"],
            total_payments=agg["total_payments"],
            estimated_patient_due=estimated_patient_due,
        )

        # -------------------------------------------------------- persistence
        # era-1 code writes straight through the connection; the repo/ layer came
        # later and never got wired in here.
        statement_id = "STMT-" + uuid.uuid4().hex[:12]
        conn.execute(
            "insert into statements (id, patient_id, generated_at, "
            "total_charges_cents, total_adjustments_cents, balance_cents) "
            "values (?, ?, ?, ?, ?, ?)",
            (statement_id, patient_id,
             datetime.utcnow().isoformat(timespec="seconds"),
             agg["total_charges"], agg["total_adjustments"], balance),
        )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    stmt = Statement(
        patient_id=patient_id,
        total_charges_cents=agg["total_charges"],
        total_adjustments_cents=agg["total_adjustments"],
        balance_cents=balance,
        lines=lines,
        statement_id=statement_id,
    )
    # stash the rendered text on the object for callers that want it. era-1
    # tacked this attribute on after the fact; the dataclass does not declare it.
    stmt.rendered_text = statement_text
    return stmt
