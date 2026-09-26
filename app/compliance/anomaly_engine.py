"""Anomaly detection over stored transactions and statements.

Covers the 16 core banking anomaly types requested by the user:
  1.  unusual_cash_withdrawals
  2.  unexpected_bank_charges
  3.  failed_reversed_transactions
  4.  unusual_transfers
  5.  abnormal_transaction_amounts
  6.  irregular_transaction_timing
  7.  missing_transactions
  8.  incorrect_balances
  9.  unusual_merchant_activity
  10. recurring_payment_anomalies
  11. unusual_international_transactions
  12. sudden_frequency_changes
  13. interest_discrepancies
  14. cheque_anomalies
  15. date_inconsistencies
  16. potential_fraud_indicators
"""
import datetime
import hashlib
import logging
import statistics
from collections import defaultdict

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.compliance import AnomalyFinding
from app.models.transaction import Transaction

logger = logging.getLogger(__name__)

ANOMALY_TYPES = {
    "unusual_cash_withdrawals": "Unusual cash withdrawals",
    "unexpected_bank_charges": "Unexpected bank charges",
    "failed_reversed_transactions": "Failed/reversed transactions",
    "unusual_transfers": "Unusual transfers",
    "abnormal_transaction_amounts": "Abnormal transaction amounts",
    "irregular_transaction_timing": "Irregular transaction timing",
    "missing_transactions": "Missing transactions",
    "incorrect_balances": "Incorrect balances",
    "unusual_merchant_activity": "Unusual merchant activity",
    "recurring_payment_anomalies": "Recurring-payment anomalies",
    "unusual_international_transactions": "Unusual international transactions",
    "sudden_frequency_changes": "Sudden changes in transaction frequency",
    "interest_discrepancies": "Interest discrepancies",
    "cheque_anomalies": "Cheque anomalies",
    "date_inconsistencies": "Date inconsistencies",
    "potential_fraud_indicators": "Potential fraud indicators",
}

def _fp(*parts) -> str:
    """Stable fingerprint so re-scanning updates instead of duplicating."""
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:64]


def _finding(user_id, anomaly_type, severity, title, detail, *, txn=None,
             amount_paise=None, occurred_on=None, evidence=None, account_id=None,
             statement_id=None, fingerprint_parts=None):
    return {
        "user_id": user_id,
        "anomaly_type": anomaly_type,
        "severity": severity,
        "title": title,
        "detail": detail,
        "transaction_id": txn.id if txn is not None else None,
        "account_id": account_id or (txn.account_id if txn is not None else None),
        "statement_id": statement_id or (txn.statement_id if txn is not None else None),
        "amount_paise": amount_paise if amount_paise is not None else (
            (txn.debit_paise or 0) + (txn.credit_paise or 0) if txn is not None else None
        ),
        "occurred_on": occurred_on or (txn.txn_date if txn is not None else None),
        "evidence": evidence or {},
        "fingerprint": _fp(anomaly_type, *(fingerprint_parts or [txn.id if txn is not None else title])),
    }


# 1. Unusual Cash Withdrawals
def _detect_unusual_cash_withdrawals(txns, user_id):
    out = []
    cash_keywords = ("ATM", "CASH WDL", "CWDR", "SELF", "ATM-WDL", "NFS*", "CASH WITHDRAWAL")
    for t in txns:
        if (t.debit_paise or 0) >= 2_00_000_00:  # >= ₹2,00,000 cash limit
            narr = (t.narration_clean or t.narration_raw or "").upper()
            if any(k in narr for k in cash_keywords) or getattr(t, "flow_type", "") == "CASH_WITHDRAWAL":
                out.append(_finding(
                    user_id, "unusual_cash_withdrawals", "high",
                    f"Large cash withdrawal: ₹{(t.debit_paise or 0) / 100:,.2f}",
                    f"Cash withdrawal of ₹{(t.debit_paise or 0) / 100:,.2f} via '{t.narration_clean or t.narration_raw}' exceeds standard cash withdrawal limits.",
                    txn=t,
                    fingerprint_parts=[t.id],
                ))
    return out


# 2. Unexpected Bank Charges
def _detect_unexpected_bank_charges(txns, user_id):
    out = []
    charge_keywords = ("PENAL", "BOUNCE CHG", "MIN BAL", "CONSOL CHG", "OD INT", "OVERDRAFT FEE", "INSUFFICIENT FUNDS FEE", "CHEQUE RETURN CHG", "ECS REJECT")
    for t in txns:
        if (t.debit_paise or 0) > 0:
            narr = (t.narration_clean or t.narration_raw or "").upper()
            if any(k in narr for k in charge_keywords):
                out.append(_finding(
                    user_id, "unexpected_bank_charges", "medium",
                    f"Unexpected penalty/fee: ₹{(t.debit_paise or 0) / 100:,.2f}",
                    f"Bank charge detected: '{t.narration_clean or t.narration_raw}'. Review for fee dispute or waiver.",
                    txn=t,
                    fingerprint_parts=[t.id],
                ))
    return out


# 3. Failed/Reversed Transactions
def _detect_failed_reversed_transactions(txns, user_id):
    out = []
    rev_keywords = ("REVERSAL", "RETURN", "BOUNCED", "REVERSED", "REV-", "DECLINE", "UNPAID")
    for t in txns:
        narr = (t.narration_clean or t.narration_raw or "").upper()
        if any(k in narr for k in rev_keywords):
            amt = (t.debit_paise or 0) + (t.credit_paise or 0)
            out.append(_finding(
                user_id, "failed_reversed_transactions", "medium",
                f"Reversed/Returned transaction: ₹{amt / 100:,.2f}",
                f"Transaction reversal/return flagged: '{t.narration_clean or t.narration_raw}'.",
                txn=t,
                fingerprint_parts=[t.id],
            ))
    return out


# 4. Unusual Transfers
def _detect_unusual_transfers(txns, user_id):
    out = []
    first_seen = {}
    ordered = sorted([t for t in txns if t.txn_date], key=lambda t: t.txn_date)
    for t in ordered:
        party = (t.counterparty or (t.narration_clean or t.narration_raw or "")[:60] or "").strip().lower()
        if not party:
            continue
        if party not in first_seen:
            first_seen[party] = t
            debit = t.debit_paise or 0
            if debit >= 5_00_000_00:  # >= ₹5,00,000 on first transfer
                out.append(_finding(
                    user_id, "unusual_transfers", "high",
                    f"First-time transfer to '{party[:35]}': ₹{debit / 100:,.2f}",
                    f"First-time high-value transfer of ₹{debit / 100:,.2f} sent to beneficiary '{party}'. Verify account details.",
                    txn=t,
                    fingerprint_parts=[t.id],
                ))
    return out


# 5. Abnormal Transaction Amounts
def _detect_abnormal_transaction_amounts(txns, user_id):
    out = []
    by_account = defaultdict(list)
    for t in txns:
        amount = (t.debit_paise or 0) + (t.credit_paise or 0)
        if amount > 0:
            by_account[t.account_id].append((amount, t))

    for account_id, rows in by_account.items():
        if len(rows) < 25:
            continue
        amounts = [a for a, _ in rows]
        mean = statistics.fmean(amounts)
        try:
            sd = statistics.stdev(amounts)
        except statistics.StatisticsError:
            continue
        if sd <= 0:
            continue
        cutoff = mean + 3.8 * sd  # High statistical significance
        for amount, t in rows:
            if amount > cutoff and amount >= 5_00_000_00:
                z = (amount - mean) / sd
                out.append(_finding(
                    user_id, "abnormal_transaction_amounts", "medium",
                    f"Abnormal transaction amount: ₹{amount / 100:,.2f} ({z:.1f}σ)",
                    f"Amount of ₹{amount / 100:,.2f} is an extreme outlier against the account average of ₹{mean / 100:,.2f}.",
                    txn=t,
                    evidence={"z_score": round(z, 2)},
                    fingerprint_parts=[t.id],
                ))
    return out


# 6. Irregular Transaction Timing
def _detect_irregular_transaction_timing(txns, user_id):
    out = []
    for t in txns:
        if t.txn_date and t.txn_date.weekday() in (5, 6) and (t.debit_paise or 0) >= 10_00_000_00:  # > ₹10 Lakhs on weekend
            out.append(_finding(
                user_id, "irregular_transaction_timing", "low",
                f"Large weekend disbursement: ₹{(t.debit_paise or 0) / 100:,.2f}",
                f"High-value payment posted on weekend ({t.txn_date.strftime('%A, %d %b %Y')}).",
                txn=t,
                fingerprint_parts=[t.id],
            ))
    return out


# 7. Missing Transactions
def _detect_missing_transactions(txns, user_id):
    out = []
    by_account = defaultdict(list)
    for t in txns:
        if t.txn_date:
            by_account[t.account_id].append(t)

    for account_id, rows in by_account.items():
        if len(rows) < 15:
            continue
        rows.sort(key=lambda t: t.txn_date)
        for prev, cur in zip(rows, rows[1:]):
            gap_days = (cur.txn_date - prev.txn_date).days
            if gap_days > 60:
                out.append(_finding(
                    user_id, "missing_transactions", "medium",
                    f"Missing activity gap of {gap_days} days",
                    f"No recorded transactions between {prev.txn_date} and {cur.txn_date}.",
                    occurred_on=cur.txn_date,
                    account_id=account_id,
                    fingerprint_parts=[account_id, prev.txn_date, cur.txn_date],
                ))
    return out


# 8. Incorrect Balances
def _detect_incorrect_balances(txns, user_id):
    """Running-balance continuity, within one ledger at a time.

    A statement is a single contiguous extract and its own row order is
    authoritative, so rows that carry a `statement_id` are compared inside that
    statement and never across two of them.

    Rows with NO statement are grouped by ACCOUNT instead. They are not a
    hypothetical: the Account Aggregator feed (`app/aa/service.py` calls
    `store_transactions` without a statement) and `POST /transactions` both
    write balance-carrying rows with `statement_id` NULL, and
    `transactions.statement_id` is ON DELETE SET NULL, so deleting a statement
    orphans the rows it ingested. Requiring a statement here meant a balance
    break on anything that did not arrive as an uploaded statement file was
    never reported at all.

    Rows with neither a statement nor an account are skipped. `resolve_account_id`
    deliberately refuses to guess when a user has several accounts, so unbound
    rows can come from different ledgers, and a balance sequence spanning two
    accounts is not a sequence.
    """
    out = []
    groups = defaultdict(list)
    for t in txns:
        if t.balance_paise is None:
            continue
        if t.statement_id:
            groups[("statement", t.statement_id)].append(t)
        elif t.account_id:
            groups[("account", t.account_id)].append(t)

    for (scope, scope_id), rows in groups.items():
        if len(rows) < 2:
            continue
        if scope == "statement":
            rows.sort(key=lambda r: (r.row_index if r.row_index is not None else 0, r.txn_date or datetime.date.min))
        else:
            # Across ingests `row_index` restarts at 0 per batch, so it cannot
            # order an account's ledger; the posting date does, with row_index
            # and then the id as tie-breaks so the pairing is deterministic
            # whatever order the rows came back from the database in.
            rows.sort(key=lambda r: (r.txn_date or datetime.date.min,
                                     r.row_index if r.row_index is not None else 0,
                                     str(r.id)))
        for prev, cur in zip(rows, rows[1:]):
            if prev.balance_paise is None or cur.balance_paise is None:
                continue
            delta = abs(cur.balance_paise - prev.balance_paise)
            amt = (cur.debit_paise or 0) + (cur.credit_paise or 0)
            drift = abs(delta - amt)
            if drift > 100 and amt > 0:
                out.append(_finding(
                    user_id, "incorrect_balances", "high",
                    f"Running balance mismatch of ₹{drift / 100:,.2f}",
                    f"Running balance break detected at {cur.txn_date}. Balance changed by ₹{delta / 100:,.2f} but transaction was ₹{amt / 100:,.2f}.",
                    txn=cur,
                    statement_id=scope_id if scope == "statement" else None,
                    fingerprint_parts=[cur.id, drift],
                ))
    return out


# 9. Unusual Merchant Activity
def _detect_unusual_merchant_activity(txns, user_id):
    out = []
    high_risk_patterns = ("CASINO", "BETTING", "DREAM11", "POKER", "CRYPTO", "BINANCE", "LOTTERY", "RUMMY")
    for t in txns:
        narr = (t.narration_clean or t.narration_raw or "").upper()
        if any(p in narr for p in high_risk_patterns):
            amt = (t.debit_paise or 0) + (t.credit_paise or 0)
            out.append(_finding(
                user_id, "unusual_merchant_activity", "high",
                f"High-risk merchant activity: ₹{amt / 100:,.2f}",
                f"Transaction with high-risk merchant pattern flagged: '{t.narration_clean or t.narration_raw}'.",
                txn=t,
                fingerprint_parts=[t.id],
            ))
    return out


# 10. Recurring-Payment Anomalies
def _detect_recurring_payment_anomalies(txns, user_id):
    out = []
    by_party = defaultdict(list)
    for t in txns:
        if (t.debit_paise or 0) > 0 and t.counterparty:
            by_party[t.counterparty.strip().lower()].append(t)

    for party, rows in by_party.items():
        if len(rows) < 5:
            continue
        amounts = [r.debit_paise for r in rows]
        median_amt = statistics.median(amounts)
        if median_amt <= 0:
            continue
        for r in rows:
            if r.debit_paise > 2.0 * median_amt and (r.debit_paise - median_amt) >= 2_00_000_00:  # > 2x and >= ₹2 Lakhs
                out.append(_finding(
                    user_id, "recurring_payment_anomalies", "medium",
                    f"Unexpected charge variation for '{party[:30]}'",
                    f"Recurring charge of ₹{r.debit_paise / 100:,.2f} is more than double the median baseline of ₹{median_amt / 100:,.2f}.",
                    txn=r,
                    fingerprint_parts=[r.id],
                ))
    return out


# 11. Unusual International Transactions
def _detect_unusual_international_transactions(txns, user_id):
    out = []
    intl_keywords = ("FOREX", "FCY", "CROSS BORDER", "INTERNATIONAL", "USD ", "EUR ", "GBP ", "AED ", "FOREIGN CURRENCY", "MARKUP FEE")
    for t in txns:
        narr = (t.narration_clean or t.narration_raw or "").upper()
        if any(k in narr for k in intl_keywords):
            amt = (t.debit_paise or 0) + (t.credit_paise or 0)
            out.append(_finding(
                user_id, "unusual_international_transactions", "medium",
                f"Foreign currency/international transaction: ₹{amt / 100:,.2f}",
                f"International exchange or foreign currency transaction detected: '{t.narration_clean or t.narration_raw}'.",
                txn=t,
                fingerprint_parts=[t.id],
            ))
    return out


# 12. Sudden Changes in Transaction Frequency
def _detect_sudden_frequency_changes(txns, user_id):
    out = []
    by_account = defaultdict(lambda: defaultdict(int))
    for t in txns:
        if t.txn_date:
            by_account[t.account_id][t.txn_date] += 1

    for account_id, per_day in by_account.items():
        if len(per_day) < 20:
            continue
        counts = list(per_day.values())
        mean = statistics.fmean(counts)
        try:
            sd = statistics.stdev(counts)
        except statistics.StatisticsError:
            continue
        if sd <= 0:
            continue
        cutoff = mean + 3.8 * sd
        for day, count in sorted(per_day.items()):
            if count > cutoff and count >= 15:
                out.append(_finding(
                    user_id, "sudden_frequency_changes", "medium",
                    f"Sharp activity surge ({count} txns on {day})",
                    f"Activity surge on {day}: {count} transactions vs average baseline of {mean:.1f} txns/day.",
                    account_id=account_id,
                    occurred_on=day,
                    fingerprint_parts=[account_id, day, count],
                ))
    return out


# 13. Interest Discrepancies
def _detect_interest_discrepancies(txns, user_id):
    out = []
    int_keywords = ("PENAL INTEREST", "INT.DR", "OVERDRAFT INTEREST")
    for t in txns:
        if (t.debit_paise or 0) > 0:
            narr = (t.narration_clean or t.narration_raw or "").upper()
            if any(k in narr for k in int_keywords):
                out.append(_finding(
                    user_id, "interest_discrepancies", "medium",
                    f"Unusual interest debit: ₹{(t.debit_paise or 0) / 100:,.2f}",
                    f"Penal or overdraft interest debit: '{t.narration_clean or t.narration_raw}'.",
                    txn=t,
                    fingerprint_parts=[t.id],
                ))
    return out


# 14. Cheque Anomalies
def _detect_cheque_anomalies(txns, user_id):
    out = []
    cheque_return_keywords = ("CHQ RTN", "CHEQUE RETURN", "DISHONOUR", "INSUFFICIENT FUNDS", "CHEQUE BOUNCE")
    for t in txns:
        narr = (t.narration_clean or t.narration_raw or "").upper()
        if any(k in narr for k in cheque_return_keywords):
            amt = (t.debit_paise or 0) + (t.credit_paise or 0)
            out.append(_finding(
                user_id, "cheque_anomalies", "high",
                f"Cheque return/dishonour: ₹{amt / 100:,.2f}",
                f"Cheque return or alteration indicator flagged: '{t.narration_clean or t.narration_raw}'.",
                txn=t,
                fingerprint_parts=[t.id],
            ))
    return out


# 15. Date Inconsistencies
def _detect_date_inconsistencies(txns, user_id):
    out = []
    today = datetime.date.today()
    for t in txns:
        if t.txn_date and t.txn_date > (today + datetime.timedelta(days=2)):
            out.append(_finding(
                user_id, "date_inconsistencies", "medium",
                f"Future transaction date: {t.txn_date}",
                f"Transaction recorded with future date ({t.txn_date}).",
                txn=t,
                fingerprint_parts=[t.id],
            ))
    return out


# 16. Potential Fraud Indicators
def _detect_potential_fraud_indicators(txns, user_id):
    out = []
    # Rapid passthrough: large credit followed by large debit within 24 hours
    by_account = defaultdict(list)
    for t in txns:
        if t.txn_date:
            by_account[t.account_id].append(t)

    for account_id, rows in by_account.items():
        rows.sort(key=lambda t: t.txn_date)
        for i, credit_txn in enumerate(rows):
            if (credit_txn.credit_paise or 0) >= 10_00_000_00:  # >= ₹10 Lakhs
                for debit_txn in rows[i+1:i+4]:
                    if (debit_txn.txn_date - credit_txn.txn_date).days <= 1:
                        if (debit_txn.debit_paise or 0) >= 0.95 * credit_txn.credit_paise:
                            out.append(_finding(
                                user_id, "potential_fraud_indicators", "critical",
                                f"Rapid deposit-to-withdrawal pass-through: ₹{debit_txn.debit_paise / 100:,.2f}",
                                f"Pass-through pattern: ₹{credit_txn.credit_paise / 100:,.2f} credit followed immediately by ₹{debit_txn.debit_paise / 100:,.2f} debit.",
                                txn=debit_txn,
                                fingerprint_parts=[credit_txn.id, debit_txn.id],
                            ))
                            break
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def detect_anomalies(db, user_id, account_id=None, date_from=None, date_to=None):
    """Run all 16 detectors and upsert findings."""
    q = db.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.superseded_by_id.is_(None),
    )
    if account_id:
        q = q.filter(Transaction.account_id == account_id)
    if date_from:
        q = q.filter(Transaction.txn_date >= date_from)
    if date_to:
        q = q.filter(Transaction.txn_date <= date_to)
    txns = q.all()

    findings = []
    for detector in (
        _detect_unusual_cash_withdrawals,
        _detect_unexpected_bank_charges,
        _detect_failed_reversed_transactions,
        _detect_unusual_transfers,
        _detect_abnormal_transaction_amounts,
        _detect_irregular_transaction_timing,
        _detect_missing_transactions,
        _detect_incorrect_balances,
        _detect_unusual_merchant_activity,
        _detect_recurring_payment_anomalies,
        _detect_unusual_international_transactions,
        _detect_sudden_frequency_changes,
        _detect_interest_discrepancies,
        _detect_cheque_anomalies,
        _detect_date_inconsistencies,
        _detect_potential_fraud_indicators,
    ):
        try:
            findings.extend(detector(txns, user_id))
        except Exception as exc:
            logger.exception("[Anomaly] detector %s failed: %s", detector.__name__, exc)

    if findings:
        stmt = pg_insert(AnomalyFinding.__table__).values(findings)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_anomaly_fingerprint",
            set_={
                "title": stmt.excluded.title,
                "detail": stmt.excluded.detail,
                "severity": stmt.excluded.severity,
                "amount_paise": stmt.excluded.amount_paise,
                "evidence": stmt.excluded.evidence,
            },
        )
        db.execute(stmt)

    _retire_superseded(db, user_id, {f["fingerprint"] for f in findings},
                       account_id, date_from, date_to)

    db.commit()
    return findings


def _retire_superseded(db, user_id, live_fingerprints, account_id, date_from, date_to):
    q = db.query(AnomalyFinding).filter(
        AnomalyFinding.user_id == user_id,
        AnomalyFinding.status.in_(("open", "acknowledged")),
    )
    if account_id:
        q = q.filter(AnomalyFinding.account_id == account_id)
    if date_from:
        q = q.filter(AnomalyFinding.occurred_on >= date_from)
    if date_to:
        q = q.filter(AnomalyFinding.occurred_on <= date_to)
    if live_fingerprints:
        q = q.filter(AnomalyFinding.fingerprint.notin_(tuple(live_fingerprints)))

    q.update(
        {
            AnomalyFinding.status: "resolved",
            AnomalyFinding.resolved_at: datetime.datetime.now(datetime.timezone.utc),
        },
        synchronize_session=False,
    )
