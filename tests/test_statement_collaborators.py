"""Tests for the three collaborators extracted from build_statement().

aggregate_claims  — DB aggregation layer
resolve_statement_rate — contract resolution
render_statement_text  — pure text renderer
"""
import uuid
from datetime import datetime

import pytest

from medclaim import db
from medclaim.billing.renderer import render_statement_text
from medclaim.billing.statement import aggregate_claims, resolve_statement_rate
from medclaim.claims import void_claim


def _add_remittance(claim_id, paid_cents, posted_date="2024-06-30"):
    conn = db.connect()
    try:
        conn.execute(
            "insert into remittances (id, claim_id, paid_cents, posted_date) "
            "values (?, ?, ?, ?)",
            ("R-" + uuid.uuid4().hex[:8], claim_id, paid_cents, posted_date),
        )
        conn.commit()
    finally:
        conn.close()


def _open_conn():
    return db.connect()


# ---------------------------------------------------------------------------
# aggregate_claims
# ---------------------------------------------------------------------------

def test_aggregate_single_claim(seed):
    seed.claim("P-2001", amount_cents=12_000, service_date="2024-06-01")
    conn = _open_conn()
    try:
        agg = aggregate_claims(conn, "P-2001")
    finally:
        conn.close()

    assert len(agg["working"]) == 1
    assert agg["total_charges"] == 12_000
    assert agg["total_adjustments"] == 0
    assert agg["total_payments"] == 0
    assert agg["newest_service_date"] == "2024-06-01"
    w = agg["working"][0]
    assert w["charge"] == 12_000
    assert w["paid"] == 0
    assert w["month"] == "2024-06"


def test_aggregate_modern_void_excluded(seed):
    claim = seed.claim("P-2001", amount_cents=9_000, service_date="2024-06-01")
    void_claim(claim.id, reason="test")
    conn = _open_conn()
    try:
        agg = aggregate_claims(conn, "P-2001")
    finally:
        conn.close()

    assert all(w["id"] != claim.id for w in agg["working"])
    assert agg["total_charges"] == 0


def test_aggregate_paid_claim_reflected(seed):
    claim = seed.claim("P-2001", amount_cents=20_000, service_date="2024-06-01")
    _add_remittance(claim.id, paid_cents=15_000)
    conn = _open_conn()
    try:
        agg = aggregate_claims(conn, "P-2001")
    finally:
        conn.close()

    assert agg["total_payments"] == 15_000
    assert agg["working"][0]["paid"] == 15_000


def test_aggregate_newest_service_date(seed):
    seed.claim("P-2001", amount_cents=1_000, service_date="2024-01-10")
    seed.claim("P-2001", amount_cents=2_000, service_date="2024-07-20")
    seed.claim("P-2001", amount_cents=3_000, service_date="2024-04-05")
    conn = _open_conn()
    try:
        agg = aggregate_claims(conn, "P-2001")
    finally:
        conn.close()

    assert agg["newest_service_date"] == "2024-07-20"
    assert agg["total_charges"] == 6_000


# ---------------------------------------------------------------------------
# resolve_statement_rate
# ---------------------------------------------------------------------------

def test_resolve_known_payer(_seeded_db):
    contract, allowed_rate, as_of = resolve_statement_rate("AETNA-002", "2024-06-01")

    assert contract is not None
    assert contract.rate_pct == 75
    assert allowed_rate == 75
    assert as_of == "2024-06-01"


def test_resolve_no_service_date_falls_back(_seeded_db):
    _, _, as_of = resolve_statement_rate("AETNA-002", None)

    # as_of must be today's date in ISO format, not empty or None
    assert as_of is not None
    assert len(as_of) == 10   # "YYYY-MM-DD"
    today = datetime.utcnow().date().isoformat()
    assert as_of == today


def test_resolve_unknown_payer(_seeded_db):
    contract, allowed_rate, as_of = resolve_statement_rate("UNKNOWN-PAYER", "2024-06-01")

    assert contract is None
    assert allowed_rate == 0


# ---------------------------------------------------------------------------
# render_statement_text
# ---------------------------------------------------------------------------

def _make_working(claim_id="C-TEST-01", charge=10_000, adjustment=0,
                  paid=0, patient_due=None, service_date="2024-06-01",
                  status="submitted"):
    """Build a minimal working-dict for renderer tests."""
    if patient_due is None:
        patient_due = max(0, charge - adjustment) if paid == 0 else 0
    return [{
        "id": claim_id,
        "service_date": service_date,
        "month": service_date[:7],
        "charge": charge,
        "adjustment": adjustment,
        "paid": paid,
        "status": status,
        "patient_due": patient_due,
        "expected_allowed": 0,
    }]


def test_render_basic_header():
    working = _make_working()
    text = render_statement_text(
        patient_id="P-TEST",
        payer_id="AETNA-002",
        contract=None,
        as_of="2024-06-01",
        allowed_rate=75,
        working=working,
        total_charges=10_000,
        total_adjustments=0,
        total_payments=0,
        estimated_patient_due=10_000,
    )

    assert "PATIENT STATEMENT" in text
    assert "P-TEST" in text
    assert "AETNA-002" in text


def test_render_payments_section():
    working = _make_working(paid=8_000, patient_due=0)
    text = render_statement_text(
        patient_id="P-TEST",
        payer_id="AETNA-002",
        contract=None,
        as_of="2024-06-01",
        allowed_rate=75,
        working=working,
        total_charges=10_000,
        total_adjustments=0,
        total_payments=8_000,
        estimated_patient_due=0,
    )

    assert "Payments received" in text
    assert "payer paid" in text
    assert "you may owe" not in text


def test_render_delinquent_flag():
    # service_date far in the past so age_days > 90
    working = _make_working(service_date="2020-01-01", patient_due=5_000)
    text = render_statement_text(
        patient_id="P-TEST",
        payer_id="AETNA-002",
        contract=None,
        as_of="2020-01-01",
        allowed_rate=75,
        working=working,
        total_charges=5_000,
        total_adjustments=0,
        total_payments=0,
        estimated_patient_due=5_000,
    )

    assert "** account past due" in text
    assert "90 days" in text


def test_render_estimated_due():
    working = _make_working(patient_due=2_500)
    text = render_statement_text(
        patient_id="P-TEST",
        payer_id="AETNA-002",
        contract=None,
        as_of="2024-06-01",
        allowed_rate=75,
        working=working,
        total_charges=10_000,
        total_adjustments=0,
        total_payments=0,
        estimated_patient_due=2_500,
    )

    assert "Estimated to owe" in text
    assert "you may owe (est)" in text
