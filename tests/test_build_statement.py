"""Safety-net tests for build_statement().

These are characterisation tests: they pin CURRENT behaviour, including
behaviour that looks wrong. Comments mark the known bugs explicitly so
reviewers understand what is being preserved, not corrected.
"""
import uuid

import pytest

from medclaim import db
from medclaim.billing.statement import build_statement
from medclaim.claims import void_claim, void_claim_legacy


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


# ---------------------------------------------------------------------------
# 1. Simple claim
# ---------------------------------------------------------------------------

def test_simple_claim(seed):
    # P-2001 → AETNA-002 → rate 75%. No seed claims for this patient.
    claim = seed.claim("P-2001", amount_cents=10_000, service_date="2024-06-01")
    stmt = build_statement("P-2001")

    assert stmt.total_charges_cents == 10_000
    assert stmt.total_adjustments_cents == 0
    assert stmt.balance_cents == 10_000
    assert len(stmt.lines) == 1
    assert stmt.statement_id.startswith("STMT-")
    # rendered_text is not a declared dataclass field; it is tacked on after
    # construction (statement.py line 329). Pin that it exists.
    assert hasattr(stmt, "rendered_text")
    assert "P-2001" in stmt.rendered_text
    # is_voided is hardcoded False for every line (active claims were already
    # filtered; voided rows never reach this code path).
    assert stmt.lines[0].is_voided is False


# ---------------------------------------------------------------------------
# 2. Multiple claims across months
# ---------------------------------------------------------------------------

def test_multiple_claims_across_months(seed):
    c1 = seed.claim("P-2001", amount_cents=5_000, service_date="2024-01-15")
    c2 = seed.claim("P-2001", amount_cents=8_000, service_date="2024-03-22")
    c3 = seed.claim("P-2001", amount_cents=12_000, service_date="2024-07-10")

    stmt = build_statement("P-2001")

    assert stmt.total_charges_cents == 25_000
    assert len(stmt.lines) == 3
    claim_ids = [line.claim_id for line in stmt.lines]
    assert c1.id in claim_ids
    assert c2.id in claim_ids
    assert c3.id in claim_ids
    # Each service month gets its own section header in the rendered output.
    assert "2024-01" in stmt.rendered_text
    assert "2024-03" in stmt.rendered_text
    assert "2024-07" in stmt.rendered_text


# ---------------------------------------------------------------------------
# 3. Adjusted claim
# ---------------------------------------------------------------------------

def test_adjusted_claim(seed):
    # AETNA rate 75%: expected_allowed = (10000 * 75) // 100 = 7500,
    # not_allowed = 2500, patient_due = 2500 - 2000 (adjustment) = 500.
    # balance = charge - adjustment - payments = 10000 - 2000 - 0 = 8000.
    seed.claim("P-2001", amount_cents=10_000, adjustment_cents=2_000,
               service_date="2024-06-01")
    stmt = build_statement("P-2001")

    assert stmt.total_adjustments_cents == 2_000
    assert stmt.balance_cents == 8_000
    assert "adjustment" in stmt.rendered_text
    # estimated_patient_due = 500 > 0, so this line is rendered.
    assert "Estimated to owe" in stmt.rendered_text


# ---------------------------------------------------------------------------
# 4. Paid claim
# ---------------------------------------------------------------------------

def test_paid_claim(seed):
    # Any non-zero payment zeroes out patient_due regardless of contract rate
    # (statement.py lines 118-119). The balance reflects actual cash flow only.
    claim = seed.claim("P-2001", amount_cents=15_000, service_date="2024-06-01")
    _add_remittance(claim.id, paid_cents=12_000)

    stmt = build_statement("P-2001")

    # balance = 15000 - 0 (no adjustment) - 12000 = 3000
    assert stmt.balance_cents == 3_000
    assert "Payments received" in stmt.rendered_text
    # patient_due is 0 for paid claims, so "you may owe" must not appear.
    assert "you may owe" not in stmt.rendered_text


# ---------------------------------------------------------------------------
# 5a. Modern void: claim is excluded from the statement
# ---------------------------------------------------------------------------

def test_modern_voided_claim_excluded(seed):
    claim = seed.claim("P-2001", amount_cents=9_000, service_date="2024-06-01")
    void_claim(claim.id, reason="test-void")

    stmt = build_statement("P-2001")

    assert claim.id not in [line.claim_id for line in stmt.lines]
    # P-2001 has no seed claims; the only claim submitted was voided.
    assert len(stmt.lines) == 0


# ---------------------------------------------------------------------------
# 5b. Legacy void: claim is still visible (known-wrong era-1 behaviour)
# ---------------------------------------------------------------------------

def test_legacy_voided_claim_appears_in_statement(seed):
    # void_claim_legacy() only writes to void_log; it does not set is_deleted.
    # Because the SQL filter is "is_deleted is not 1", a legacy-voided claim
    # (is_deleted = 0) passes through and appears on the statement.
    # This is the documented era-1 behaviour, pinned here intentionally.
    claim = seed.claim("P-2001", amount_cents=9_000, service_date="2024-06-01")
    void_claim_legacy(claim.id, reason="test-legacy-void")

    stmt = build_statement("P-2001")

    assert claim.id in [line.claim_id for line in stmt.lines]
    # is_voided is always hardcoded False — the dataclass field is never set
    # to True anywhere in build_statement.
    assert stmt.lines[0].is_voided is False


# ---------------------------------------------------------------------------
# 6. Claim near a contract boundary date (wrong rate applied)
# ---------------------------------------------------------------------------

def test_bcbs_2023_claim_uses_wrong_contract_rate(seed):
    # BCBS-001 has two contracts: 2023-01-01→2023-12-31 at 80%, and
    # 2024-01-01→open at 70%. resolve_contract() ignores service_date and
    # calls current_contract(), which returns the latest row by effective_date
    # DESC — always the 2024 contract (70%). A claim dated 2023-06-15 should
    # resolve to 80% but instead gets 70% due to the bug in contracts.py line 59.
    #
    # Math under the bug (rate=70%):
    #   expected_allowed = (10000 * 70) // 100 = 7000
    #   patient_due      = 10000 - 7000 = 3000  (correct would be 2000 at 80%)
    #
    # P-1042 has two seed claims (service dates in 2024), so newest_service_date
    # is 2024-04-11; the as_of date in the header reflects that, not 2023-06-15.
    seed.claim("P-1042", amount_cents=10_000, service_date="2023-06-15")
    stmt = build_statement("P-1042")

    # The header "Contract as of 2024-04-11: 70% allowed" reveals the wrong rate.
    assert "70%" in stmt.rendered_text
    assert "80%" not in stmt.rendered_text

    # The per-claim "you may owe" line reflects patient_due=3000 (70% rate),
    # not the 2000 that the correct 2023 contract (80%) would produce.
    # Format string: "      you may owe (est) %9d" (statement.py line 212).
    assert "you may owe (est)      3000" in stmt.rendered_text
    assert "you may owe (est)      2000" not in stmt.rendered_text
