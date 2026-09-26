import io
import csv
import uuid
from typing import Dict, List, Optional
from datetime import date as date_type, datetime
import pandas as pd
from fastapi import APIRouter, Depends, Query, Response, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, extract

from app.database.session import get_db
from app.services.cache import cache
from app.models.user import User
from app.models.transaction import Transaction
from app.utils.security import get_current_user
from app.api import treasury_periods
from app.api.scoping import entity_scope

router = APIRouter(prefix="/reports", tags=["Reports & Export"])

#: Short for the same reason the dashboard's is: invalidation on write is the
#: mechanism, and this only bounds the damage if an invalidation is ever missed.
_TREASURY_TTL_SECONDS = 30


# ---------------------------------------------------------------------------
# Internal helper — canonical active transactions base query
# ---------------------------------------------------------------------------

def _active_txns(db: Session, user_id):
    """Return a base query for active (non-superseded) canonical transactions."""
    return db.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.superseded_by_id == None,
    )


def _csv_safe(value):
    """Neutralise spreadsheet formula injection in exported CSV cells.

    Narration and reference text originates from parsed bank statements and
    emailed attachments, so it is attacker-influenced. Excel/Sheets execute any
    cell beginning with = + - @ (or a leading tab/CR), which turns an exported
    report into code execution on the finance team's machine. Prefixing with a
    single quote keeps the value visible while forcing it to be treated as text.
    """
    if value is None or not isinstance(value, str):
        return value
    if value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _txn_to_row(t: Transaction) -> dict:
    """Convert a canonical Transaction to a serialisable dict for reports."""
    deb  = (t.debit_paise or 0) / 100.0
    cred = (t.credit_paise or 0) / 100.0
    amt  = deb if deb > 0 else cred
    bal  = (t.balance_paise or 0) / 100.0

    cat_name = "Uncategorized"
    conf     = None
    source   = t.source_channel or "STATEMENT"

    if t.category_node:
        cat_name = t.category_node.name
    elif t.prediction:
        cat_name = t.prediction.predicted_category or "Uncategorized"
        conf     = t.prediction.confidence

    return {
        "id":               str(t.id),
        "date":             t.txn_date.strftime("%Y-%m-%d") if t.txn_date else None,
        "description":      t.narration_clean or t.narration_raw or "",
        "debit":            round(deb, 2),
        "credit":           round(cred, 2),
        "amount":           round(amt, 2),
        "balance":          round(bal, 2),
        "transaction_type": t.payment_method or t.direction.value,
        "reference_number": t.reference_no or "",
        "category":         cat_name,
        "confidence":       conf,
        "source":           source,
    }


# =====================================================================
# 1. REPORT GENERATION REST ENDPOINTS (Canonical Transactions)
# =====================================================================

@router.get("/monthly")
def get_monthly_report(
    year: int = Query(2026),
    month: int = Query(..., ge=1, le=12),
    entity_id: Optional[uuid.UUID] = Query(None),
    account_id: Optional[uuid.UUID] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = _active_txns(db, current_user.id).filter(
        extract("year",  Transaction.txn_date) == year,
        extract("month", Transaction.txn_date) == month,
    )
    if entity_id:
        query = query.filter(entity_scope(entity_id))
    if account_id:
        query = query.filter(Transaction.account_id == account_id)

    txns = query.order_by(Transaction.txn_date.asc()).all()

    # Paise arithmetic
    total_debit_paise  = sum(t.debit_paise  or 0 for t in txns)
    total_credit_paise = sum(t.credit_paise or 0 for t in txns)
    total_debit  = total_debit_paise  / 100.0
    total_credit = total_credit_paise / 100.0

    return {
        "report_type":       "Monthly Report",
        "period":            f"{year}-{month:02d}",
        "transaction_count": len(txns),
        "total_debit":       round(total_debit, 2),
        "total_credit":      round(total_credit, 2),
        "net_cash_flow":     round(total_credit - total_debit, 2),
        "transactions":      [_txn_to_row(t) for t in txns],
    }


@router.get("/yearly")
def get_yearly_report(
    year: int = Query(2026),
    entity_id: Optional[uuid.UUID] = Query(None),
    account_id: Optional[uuid.UUID] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = _active_txns(db, current_user.id).filter(
        extract("year", Transaction.txn_date) == year
    )
    if entity_id:
        query = query.filter(entity_scope(entity_id))
    if account_id:
        query = query.filter(Transaction.account_id == account_id)

    txns = query.order_by(Transaction.txn_date.asc()).all()

    total_debit_paise  = sum(t.debit_paise  or 0 for t in txns)
    total_credit_paise = sum(t.credit_paise or 0 for t in txns)
    total_debit  = total_debit_paise  / 100.0
    total_credit = total_credit_paise / 100.0

    return {
        "report_type":       "Yearly Report",
        "year":              year,
        "transaction_count": len(txns),
        "total_debit":       round(total_debit, 2),
        "total_credit":      round(total_credit, 2),
        "net_cash_flow":     round(total_credit - total_debit, 2),
        "transactions":      [_txn_to_row(t) for t in txns],
    }


@router.get("/expense")
def get_expense_report(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    entity_id: Optional[uuid.UUID] = Query(None),
    account_id: Optional[uuid.UUID] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = _active_txns(db, current_user.id).filter(
        Transaction.debit_paise > 0
    )
    if start_date:
        query = query.filter(
            Transaction.txn_date >= datetime.strptime(start_date, "%Y-%m-%d").date()
        )
    if end_date:
        query = query.filter(
            Transaction.txn_date <= datetime.strptime(end_date, "%Y-%m-%d").date()
        )
    if entity_id:
        query = query.filter(entity_scope(entity_id))
    if account_id:
        query = query.filter(Transaction.account_id == account_id)

    txns = query.order_by(Transaction.txn_date.desc()).all()
    total_expense = sum(t.debit_paise or 0 for t in txns) / 100.0

    return {
        "report_type":   "Expense Report",
        "total_expense": round(total_expense, 2),
        "count":         len(txns),
        "expenses": [
            {
                "id":          str(t.id),
                "date":        t.txn_date.strftime("%Y-%m-%d") if t.txn_date else None,
                "description": t.narration_clean or t.narration_raw or "",
                "amount":      round((t.debit_paise or 0) / 100.0, 2),
                "category":    t.category_node.name if t.category_node else (
                    t.prediction.predicted_category if t.prediction else "Uncategorized"
                ),
            }
            for t in txns
        ],
    }


@router.get("/income")
def get_income_report(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    entity_id: Optional[uuid.UUID] = Query(None),
    account_id: Optional[uuid.UUID] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = _active_txns(db, current_user.id).filter(
        Transaction.credit_paise > 0
    )
    if start_date:
        query = query.filter(
            Transaction.txn_date >= datetime.strptime(start_date, "%Y-%m-%d").date()
        )
    if end_date:
        query = query.filter(
            Transaction.txn_date <= datetime.strptime(end_date, "%Y-%m-%d").date()
        )
    if entity_id:
        query = query.filter(entity_scope(entity_id))
    if account_id:
        query = query.filter(Transaction.account_id == account_id)

    txns = query.order_by(Transaction.txn_date.desc()).all()
    total_income = sum(t.credit_paise or 0 for t in txns) / 100.0

    return {
        "report_type":  "Income Report",
        "total_income": round(total_income, 2),
        "count":        len(txns),
        "incomes": [
            {
                "id":          str(t.id),
                "date":        t.txn_date.strftime("%Y-%m-%d") if t.txn_date else None,
                "description": t.narration_clean or t.narration_raw or "",
                "amount":      round((t.credit_paise or 0) / 100.0, 2),
                "category":    t.category_node.name if t.category_node else (
                    t.prediction.predicted_category if t.prediction else "Uncategorized"
                ),
            }
            for t in txns
        ],
    }


# =====================================================================
# 2. REPORT EXPORT ENDPOINTS (CSV, Excel, PDF)
# =====================================================================

def _filter_export_txns(
    db: Session,
    user_id,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    entity_id: Optional[str] = None,
    account_id: Optional[str] = None
):
    query = _active_txns(db, user_id)
    if start_date:
        try:
            s_date = datetime.strptime(start_date, "%Y-%m-%d").date()
            query = query.filter(Transaction.txn_date >= s_date)
        except ValueError:
            pass
    if end_date:
        try:
            e_date = datetime.strptime(end_date, "%Y-%m-%d").date()
            query = query.filter(Transaction.txn_date <= e_date)
        except ValueError:
            pass
    if entity_id:
        try:
            target_ent = uuid.UUID(entity_id)
            query = query.filter(entity_scope(target_ent))
        except ValueError:
            pass
    if account_id:
        try:
            target_acc = uuid.UUID(account_id)
            query = query.filter(Transaction.account_id == target_acc)
        except ValueError:
            pass
    return query.order_by(Transaction.txn_date.desc()).all()


@router.get("/export/csv")
def export_csv(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    entity_id: Optional[str] = Query(None),
    account_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    txns = _filter_export_txns(db, current_user.id, start_date, end_date, entity_id, account_id)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Date", "Description", "Debit", "Credit", "Amount", "Balance",
        "Transaction Type", "Reference Number", "Category", "Confidence", "Source"
    ])

    for t in txns:
        row = _txn_to_row(t)
        writer.writerow([
            row["date"],
            _csv_safe(row["description"]),
            f"{float(row['debit']):.2f}",
            f"{float(row['credit']):.2f}",
            f"{float(row['amount']):.2f}",
            f"{float(row['balance']):.2f}",
            _csv_safe(row["transaction_type"]),
            _csv_safe(row["reference_number"]),
            _csv_safe(row["category"]),
            row["confidence"],
            _csv_safe(row["source"]),
        ])

    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=transactions_report.csv"}
    )


@router.get("/export/excel")
def export_excel(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    entity_id: Optional[str] = Query(None),
    account_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    txns = _filter_export_txns(db, current_user.id, start_date, end_date, entity_id, account_id)

    data = []
    for t in txns:
        row = _txn_to_row(t)
        data.append({
            "Date":             row["date"],
            "Description":      row["description"],
            "Debit":            row["debit"],
            "Credit":           row["credit"],
            "Amount":           row["amount"],
            "Balance":          row["balance"],
            "Transaction Type": row["transaction_type"],
            "Reference Number": row["reference_number"],
            "Category":         row["category"],
            "Confidence":       row["confidence"],
            "Source":           row["source"],
        })

    df = pd.DataFrame(data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Transactions")

    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=transactions_report.xlsx"}
    )


@router.get("/export/pdf")
def export_pdf(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    entity_id: Optional[str] = Query(None),
    account_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    txns = _filter_export_txns(db, current_user.id, start_date, end_date, entity_id, account_id)

    lines = [
        "FINANCIAL TRANSACTIONS REPORT",
        f"Generated on: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC",
        "=" * 80,
        f"{'Date':<12} | {'Description':<30} | {'Debit':<10} | {'Credit':<10} | {'Category':<15}",
        "-" * 80
    ]

    total_debit_paise  = 0
    total_credit_paise = 0

    for t in txns:
        row = _txn_to_row(t)
        d_str     = row["date"] or ""
        desc      = row["description"] or ""
        desc_short = (desc[:27] + "...") if len(desc) > 30 else desc
        cat_short = row["category"][:15]
        lines.append(
            f"{d_str:<12} | {desc_short:<30} | {row['debit']:<10.2f} | {row['credit']:<10.2f} | {cat_short:<15}"
        )
        total_debit_paise  += (t.debit_paise or 0)
        total_credit_paise += (t.credit_paise or 0)

    total_debit  = total_debit_paise  / 100.0
    total_credit = total_credit_paise / 100.0

    lines.append("=" * 80)
    lines.append(
        f"Total Debit: {total_debit:.2f} | Total Credit: {total_credit:.2f} | "
        f"Net Cash Flow: {total_credit - total_debit:.2f}"
    )

    pdf_content = "\n".join(lines).encode("utf-8")

    return StreamingResponse(
        io.BytesIO(pdf_content),
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=transactions_report.pdf"}
    )


# =====================================================================
# 3. TREASURY OVERVIEW REPORT & CSV EXPORT
# =====================================================================

from app.models.entity import Entity
from app.models.account import Account



def _line_item_for(t) -> str:
    """Which line of the treasury report this transaction belongs on.

    ONE resolver, called by the group, entity and account loops alike. They
    used to spell this out separately, which is how the account columns and the
    entity column above them could have disagreed about a row — and a report
    whose sub-totals do not add up to its totals is worse than one that omits
    them.

    `category` is the tree's level 1; `legacy_category` is the older vocabulary
    the P&L lines were built from and is still what most existing rows carry.
    The linked Category row is the fallback for anything ingested before either.
    """
    if getattr(t, "category", None):
        return t.category
    if getattr(t, "legacy_category", None):
        return t.legacy_category
    if t.category_node:
        return t.category_node.name
    if t.prediction and t.prediction.predicted_category:
        return t.prediction.predicted_category
    return "Uncategorized"


def _account_balances(period_txns, prior_txns, acc_id):
    """Opening and closing for one account, preferring the bank's own balance.

    THIS IS THE POINT OF THE FUNCTION. Opening used to be the *sum of net
    movement before the period*, which is 0 whenever there is no earlier data —
    so closing collapsed to "every rupee that ever moved through this account"
    and was reported as a cash position. On a year of seeded data that read
    -2,60,26,449 against an actual statement balance of -22,79,434: out by an
    order of magnitude, and confidently formatted.

    A statement carries a running balance. That balance IS the cash position;
    movement is what changed it. Sources, best first:

    1. **reported** — the balance the bank last stated before the period began.
       Nothing beats the bank saying so.
    2. **movement** — no balance before the period, but there are earlier
       transactions. Summing them is a real signal, though it assumes the
       account's history is complete back to inception.
    3. **reconstructed** — no earlier transactions at all. The period opening is
       recovered by reversing every in-period line up to and including the first
       one that carries a balance. Note *every* line: reversing only the balanced
       row returns the balance just before that row, which is not the period
       opening unless it happens to be the first row. Getting this wrong on the
       golden fixture returned 12,000 where the answer was 10,000.
    4. **derived** — no balances anywhere. Movement arithmetic is all there is,
       and the caller is told so.

    Order 2 before 3 deliberately: reconstruction inherits any inconsistency in
    the statement it reads from, so it is a fallback for accounts with no
    history rather than an upgrade over history we actually hold.

    Returns (opening_paise, reported_closing_paise, basis). The caller reports
    `opening + inflows - outflows` as the closing balance so the statement
    identity always holds, and carries the reported figure beside it: where the
    two differ the statements do not fully explain the change in cash, and that
    gap is a finding to show, not a total to quietly overwrite.
    """
    def _key(t):
        return (t.txn_date, t.row_index if t.row_index is not None else 0)

    prior = sorted((t for t in prior_txns if t.account_id == acc_id), key=_key)
    prior_with_bal = [t for t in prior if t.balance_paise is not None]
    period_all = sorted((t for t in period_txns if t.account_id == acc_id), key=_key)
    period_with_bal = [t for t in period_all if t.balance_paise is not None]

    movement = sum((t.credit_paise or 0) - (t.debit_paise or 0) for t in period_all)

    closing = (period_with_bal[-1].balance_paise if period_with_bal
               else (prior_with_bal[-1].balance_paise if prior_with_bal else None))

    if prior_with_bal:
        opening = prior_with_bal[-1].balance_paise
        basis = "reported"
    elif prior:
        opening = sum((t.credit_paise or 0) - (t.debit_paise or 0) for t in prior)
        basis = "movement"
    elif period_with_bal:
        first = period_with_bal[0]
        upto = period_all[:period_all.index(first) + 1]
        opening = first.balance_paise - sum(
            (t.credit_paise or 0) - (t.debit_paise or 0) for t in upto)
        basis = "reconstructed"
    else:
        return 0, movement, "derived"

    if closing is None:
        closing = opening + movement
    return opening, closing, basis


def compute_treasury_overview(
    db: Session,
    user_id,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    entity_id: Optional[str] = None,
    account_id: Optional[str] = None,
    period: Optional[str] = None,
) -> dict:
    """Treasury overview for a period, with entity and account detail.

    `period` is a named preset (see `treasury_periods`); `start_date`/`end_date`
    still work and still win, so every existing caller behaves exactly as before.
    """
    # Cached here rather than at the endpoint so the CSV export shares it: both
    # callers ask this function the same question with the same arguments, and
    # it is a pure read — it returns a plain dict and writes nothing.
    #
    # Today's date is part of the key because a named period is relative to it:
    # "current_month" resolves to a different window tomorrow, and an entry that
    # outlived midnight would report last month's figures under this month's
    # heading.
    overview_key = cache.user_key(user_id, "reports-treasury", start_date,
                                  end_date, entity_id, account_id, period,
                                  date_type.today())
    cached_overview = cache.get(overview_key)
    if cached_overview is not None:
        return cached_overview

    resolved = treasury_periods.resolve(period, start_date, end_date)
    s_date, e_date = resolved.start, resolved.end

    entities_query = db.query(Entity).filter(
        Entity.user_id == user_id,
        Entity.is_active == True
    )
    if entity_id and isinstance(entity_id, str):
        try:
            target_ent_uuid = uuid.UUID(entity_id)
            entities_query = entities_query.filter(Entity.id == target_ent_uuid)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid entity_id format")

    entities = entities_query.all()

    accounts_query = db.query(Account).filter(
        Account.user_id == user_id,
        Account.deleted_at == None
    )
    if account_id and isinstance(account_id, str):
        try:
            target_acc_uuid = uuid.UUID(account_id)
            accounts_query = accounts_query.filter(Account.id == target_acc_uuid)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid account_id format")

    all_accounts = accounts_query.all()

    entity_buckets = []
    if entities:
        for ent in entities:
            ent_accs = [a for a in all_accounts if a.entity_id == ent.id]
            entity_buckets.append({
                "id": str(ent.id),
                "name": ent.name,
                "accounts": ent_accs
            })

    unassigned_accs = [a for a in all_accounts if a.entity_id is None]
    if unassigned_accs or not entity_buckets:
        if not entity_buckets and not unassigned_accs:
            entity_buckets.append({
                "id": "default",
                "name": "Default Entity",
                "accounts": []
            })
        elif unassigned_accs:
            entity_buckets.append({
                "id": "unassigned",
                "name": "Default / Unassigned Entity",
                "accounts": unassigned_accs
            })

    entity_results = []
    group_opening_paise = 0
    group_inflows_paise = 0
    group_outflows_paise = 0
    group_min_breaches = 0
    group_uncategorized_count = 0

    group_reported_closing_paise = 0
    group_bases = set()
    group_inflow_cats = {}
    group_outflow_cats = {}

    import uuid as uuid_mod

    for bucket in entity_buckets:
        acc_ids = [a.id for a in bucket["accounts"]]

        txns_q = _active_txns(db, user_id)
        if acc_ids:
            txns_q = txns_q.filter(Transaction.account_id.in_(acc_ids))
        elif bucket["id"] not in ("default", "unassigned"):
            try:
                txns_q = txns_q.filter(entity_scope(uuid_mod.UUID(bucket["id"])))
            except ValueError:
                # An unparseable bucket id must not silently widen the query to
                # the whole ledger — scope it to nothing instead.
                txns_q = txns_q.filter(False)
        else:
            # The synthetic default/unassigned bucket. Previously NEITHER branch
            # ran here, so txns_q stayed unfiltered and this bucket summed the
            # user's entire ledger while the response echoed back the requested
            # entity/account — a filtered report showing unfiltered money.
            txns_q = txns_q.filter(Transaction.account_id == None)
            if entity_id or account_id:
                # An explicit filter was requested but resolved to no accounts,
                # so the correct answer is an empty bucket, not everything.
                txns_q = txns_q.filter(False)

        opening_paise = 0
        prior_txns = []
        if s_date:
            prior_txns = txns_q.filter(Transaction.txn_date < s_date).all()
            opening_paise = sum((t.credit_paise or 0) - (t.debit_paise or 0) for t in prior_txns)

        period_q = txns_q
        if s_date:
            period_q = period_q.filter(Transaction.txn_date >= s_date)
        if e_date:
            period_q = period_q.filter(Transaction.txn_date <= e_date)

        period_txns = period_q.all()

        inflows_paise = 0
        outflows_paise = 0
        inflow_cats = {}
        outflow_cats = {}
        uncategorized_count = 0

        for t in period_txns:
            cat_name = _line_item_for(t)

            if cat_name == "Uncategorized":
                uncategorized_count += 1

            deb = t.debit_paise or 0
            cred = t.credit_paise or 0

            if cred > 0:
                inflows_paise += cred
                inflow_cats[cat_name] = inflow_cats.get(cat_name, 0) + cred
                group_inflow_cats[cat_name] = group_inflow_cats.get(cat_name, 0) + cred

            if deb > 0:
                outflows_paise += deb
                outflow_cats[cat_name] = outflow_cats.get(cat_name, 0) + deb
                group_outflow_cats[cat_name] = group_outflow_cats.get(cat_name, 0) + deb


        min_breaches = 0
        for acc in bucket["accounts"]:
            if getattr(acc, "min_balance_paise", None):
                threshold = acc.min_balance_paise
                acc_period_txns = [t for t in period_txns if t.account_id == acc.id and t.balance_paise is not None]
                for t in acc_period_txns:
                    if t.balance_paise < threshold:
                        min_breaches += 1

        # Per-account detail: the hierarchy's missing rung. The page could show
        # Group and Entity but stopped there, so "which account is short" — the
        # question a treasury user actually opens this report to answer — had no
        # answer on the screen. Computed from the same period_txns the entity
        # totals come from, so the account rows always sum to their entity.
        account_results = []
        for acc in bucket["accounts"]:
            acc_period = [t for t in period_txns if t.account_id == acc.id]
            acc_in = sum(t.credit_paise or 0 for t in acc_period)
            acc_out = sum(t.debit_paise or 0 for t in acc_period)

            # The same breakdown the entity column gets. Its absence was the
            # whole reason every account column read "—" on a report whose
            # entity column was full of money: the page could show WHICH ENTITY
            # spent on Cost of Goods and not WHICH ACCOUNT it left, which is the
            # question a treasury user opens the report to answer.
            #
            # Built with the same `_line_item_for` the entity loop uses, so an
            # account row can never disagree with the entity row above it about
            # which line a transaction belongs on.
            acc_inflow_cats: Dict[str, int] = {}
            acc_outflow_cats: Dict[str, int] = {}
            for t in acc_period:
                name = _line_item_for(t)
                if (t.credit_paise or 0) > 0:
                    acc_inflow_cats[name] = acc_inflow_cats.get(name, 0) + t.credit_paise
                if (t.debit_paise or 0) > 0:
                    acc_outflow_cats[name] = acc_outflow_cats.get(name, 0) + t.debit_paise

            acc_opening, acc_reported_closing, basis = _account_balances(
                period_txns, prior_txns, acc.id)

            # Closing keeps the statement identity: opening + in - out. The
            # bank's own figure rides alongside so a disagreement is visible
            # rather than silently deciding the total.
            acc_closing = acc_opening + acc_in - acc_out

            latest = max(
                (t for t in acc_period if t.balance_paise is not None),
                key=lambda t: (t.txn_date, t.row_index or 0), default=None,
            )
            threshold = getattr(acc, "min_balance_paise", None)
            below = [t for t in acc_period
                     if t.balance_paise is not None and threshold and t.balance_paise < threshold]

            acc_uncat = sum(1 for t in acc_period
                            if _line_item_for(t) == "Uncategorized")

            detailed_acc_breaches = [
                {
                    "account_id": str(acc.id),
                    "account_label": acc.account_label or f"{acc.bank_code} {acc.account_number_masked}",
                    "date": t.txn_date.isoformat(),
                    "threshold": round(threshold / 100.0, 2),
                    "balance": round(t.balance_paise / 100.0, 2),
                    "shortfall": round((threshold - t.balance_paise) / 100.0, 2),
                }
                for t in below
            ]

            account_results.append({
                "account_id": str(acc.id),
                "account_label": (acc.account_label
                                  or f"{acc.bank_code} {acc.account_number_masked}"),
                "bank_code": acc.bank_code,
                "account_number_masked": acc.account_number_masked,
                "account_type": acc.account_type,
                "currency": acc.currency or "INR",
                "opening_balance": round(acc_opening / 100.0, 2),
                "total_inflows": round(acc_in / 100.0, 2),
                "total_outflows": round(acc_out / 100.0, 2),
                "net_flow": round((acc_in - acc_out) / 100.0, 2),
                "closing_balance": round(acc_closing / 100.0, 2),
                "balance_basis": basis,
                "unexplained_difference": round(
                    (acc_reported_closing - acc_closing) / 100.0, 2),
                "reported_closing_balance": (round(latest.balance_paise / 100.0, 2)
                                             if latest is not None else None),
                "min_balance_required": (round(threshold / 100.0, 2) if threshold else None),
                "lowest_balance": (round(min(t.balance_paise for t in acc_period
                                             if t.balance_paise is not None) / 100.0, 2)
                                   if any(t.balance_paise is not None for t in acc_period) else None),
                "min_balance_breaches": len(below),
                "breach_dates": sorted({t.txn_date.isoformat() for t in below})[:10],
                "breach_details": detailed_acc_breaches,
                "uncategorized_count": acc_uncat,
                "transaction_count": len(acc_period),
                "inflow_categories": {k: round(v / 100.0, 2)
                                      for k, v in acc_inflow_cats.items()},
                "outflow_categories": {k: round(v / 100.0, 2)
                                       for k, v in acc_outflow_cats.items()},
            })

        net_flow_paise = inflows_paise - outflows_paise

        if bucket["accounts"] and any(a["balance_basis"] != "derived" for a in account_results):
            opening_paise = sum(int(round(a["opening_balance"] * 100)) for a in account_results)
            bases = {a["balance_basis"] for a in account_results}
            basis = bases.pop() if len(bases) == 1 else "mixed"
        else:
            basis = "derived"

        closing_paise = opening_paise + net_flow_paise
        reported_closing_paise = sum(
            int(round((a["closing_balance"] + a["unexplained_difference"]) * 100))
            for a in account_results) if account_results else closing_paise

        all_entity_breaches = []
        for a in account_results:
            all_entity_breaches.extend(a.get("breach_details", []))

        entity_results.append({
            "entity_id": bucket["id"],
            "entity_name": bucket["name"],
            "accounts": account_results,
            "opening_balance": round(opening_paise / 100.0, 2),
            "total_inflows": round(inflows_paise / 100.0, 2),
            "total_outflows": round(outflows_paise / 100.0, 2),
            "net_flow": round(net_flow_paise / 100.0, 2),
            "closing_balance": round(closing_paise / 100.0, 2),
            "unexplained_difference": round(
                (reported_closing_paise - closing_paise) / 100.0, 2),
            "balance_basis": basis,
            "min_balance_breaches": min_breaches,
            "breach_details": all_entity_breaches,
            "uncategorized_count": uncategorized_count,
            "inflow_categories": {k: round(v / 100.0, 2) for k, v in inflow_cats.items()},
            "outflow_categories": {k: round(v / 100.0, 2) for k, v in outflow_cats.items()}
        })

        group_opening_paise += opening_paise
        group_reported_closing_paise += reported_closing_paise
        group_bases.add(basis)
        group_inflows_paise += inflows_paise
        group_outflows_paise += outflows_paise
        group_min_breaches += min_breaches
        group_uncategorized_count += uncategorized_count

    group_net_flow_paise = group_inflows_paise - group_outflows_paise
    group_closing_paise = group_opening_paise + group_net_flow_paise

    # All active non-superseded transactions for historical calculations and forecasting
    all_active_txns = _active_txns(db, user_id).all()

    span = db.query(
        func.min(Transaction.txn_date), func.max(Transaction.txn_date)
    ).filter(
        Transaction.user_id == user_id,
        Transaction.superseded_by_id.is_(None),
    ).first()
    first_txn, last_txn = (span or (None, None))
    history_days = max(1, (last_txn - first_txn).days) if (first_txn and last_txn) else 30

    # 1. Previous Period Comparison
    prev_net_flow_paise = 0
    prev_inflows_paise = 0
    prev_outflows_paise = 0
    prev_txns = []
    if resolved.prev_start and resolved.prev_end:
        prev_q = _active_txns(db, user_id).filter(
            Transaction.txn_date >= resolved.prev_start,
            Transaction.txn_date <= resolved.prev_end,
        )
        prev_txns = prev_q.all()
        prev_inflows_paise = sum(t.credit_paise or 0 for t in prev_txns)
        prev_outflows_paise = sum(t.debit_paise or 0 for t in prev_txns)
        prev_net_flow_paise = prev_inflows_paise - prev_outflows_paise

    net_delta_paise = group_net_flow_paise - prev_net_flow_paise
    net_delta_pct = (
        round((net_delta_paise / abs(prev_net_flow_paise)) * 100.0, 1)
        if prev_net_flow_paise != 0 else None
    )
    net_delta_direction = "up" if net_delta_paise > 0 else ("down" if net_delta_paise < 0 else "flat")

    # 2. Runway and Daily Cash Metrics
    group_tot_out = sum(t.debit_paise or 0 for t in all_active_txns)
    group_tot_in = sum(t.credit_paise or 0 for t in all_active_txns)
    group_net_burn = group_tot_out - group_tot_in
    group_monthly_burn_paise = int((group_net_burn / history_days) * 30.4) if (group_net_burn > 0 and history_days > 0) else 0

    if group_monthly_burn_paise > 0 and group_closing_paise > 0:
        group_runway_months = round(group_closing_paise / group_monthly_burn_paise, 1)
        group_runway_status = f"{group_runway_months} months"
    elif group_closing_paise <= 0:
        group_runway_months = 0.0
        group_runway_status = "Zero / Negative Cash"
    else:
        group_runway_months = None
        group_runway_status = "Cash-flow positive"

    # Per-Entity Extended Metrics
    for e in entity_results:
        e_closing_paise = int(round(e["closing_balance"] * 100))
        e["pct_of_group_cash"] = (
            round((e_closing_paise / group_closing_paise) * 100.0, 1)
            if group_closing_paise > 0 else 0.0
        )

        ent_acc_ids = {uuid_mod.UUID(a["account_id"]) for a in e.get("accounts", []) if a.get("account_id")}
        e_prev_txns = [
            t for t in prev_txns
            if (t.account_id in ent_acc_ids) or (e["entity_id"] not in ("default", "unassigned") and str(t.entity_id) == e["entity_id"])
        ]
        e_prev_net_paise = sum((t.credit_paise or 0) - (t.debit_paise or 0) for t in e_prev_txns)
        e_curr_net_paise = int(round(e["net_flow"] * 100))
        e["net_flow_pct_change"] = (
            round(((e_curr_net_paise - e_prev_net_paise) / abs(e_prev_net_paise)) * 100.0, 1)
            if (e_prev_net_paise and e_prev_net_paise != 0) else None
        )

        e_all_txns = [
            t for t in all_active_txns
            if (t.account_id in ent_acc_ids) or (e["entity_id"] not in ("default", "unassigned") and str(t.entity_id) == e["entity_id"])
        ]
        e_tot_out = sum(t.debit_paise or 0 for t in e_all_txns)
        e_tot_in = sum(t.credit_paise or 0 for t in e_all_txns)
        e_daily_outflow_paise = int(e_tot_out / history_days) if history_days > 0 else 0
        e["days_of_cash"] = (
            round(e_closing_paise / e_daily_outflow_paise, 1)
            if (e_daily_outflow_paise > 0 and e_closing_paise > 0)
            else (None if e_closing_paise <= 0 else 999.0)
        )

        e_burn = e_tot_out - e_tot_in
        e_monthly_burn = int((e_burn / history_days) * 30.4) if (e_burn > 0 and history_days > 0) else 0
        if e_monthly_burn > 0 and e_closing_paise > 0:
            e["cash_runway"] = f"{round(e_closing_paise / e_monthly_burn, 1)} months"
        elif e_closing_paise <= 0:
            e["cash_runway"] = "Zero / Negative Cash"
        else:
            e["cash_runway"] = "Cash-flow positive"

        # Per-account percentage of entity cash
        for a in e.get("accounts", []):
            a_closing_paise = int(round(a["closing_balance"] * 100))
            a["pct_of_entity_cash"] = (
                round((a_closing_paise / e_closing_paise) * 100.0, 1)
                if e_closing_paise > 0 else 0.0
            )
            a["pct_of_group_cash"] = (
                round((a_closing_paise / group_closing_paise) * 100.0, 1)
                if group_closing_paise > 0 else 0.0
            )

    # 3. Monthly Net Cash Flow Trend (trailing 6 to 12 months)
    ref_end_date = e_date or (last_txn if last_txn else date_type.today())
    monthly_trend = []
    # Build list of 12 month buckets up to ref_end_date
    curr_yr, curr_mo = ref_end_date.year, ref_end_date.month
    months_to_build = []
    for i in range(11, -1, -1):
        m = curr_mo - i
        y = curr_yr
        while m <= 0:
            m += 12
            y -= 1
        months_to_build.append((y, m))

    # Bucket the ledger by (year, month) once rather than re-filtering the whole
    # list for each of the twelve months. Same predicate, same arithmetic — but
    # the old form walked every active transaction twelve times, which on a year
    # of data was the single largest block of Python in this report.
    monthly_buckets: Dict[tuple, list] = {}
    for t in all_active_txns:
        if t.txn_date:
            monthly_buckets.setdefault(
                (t.txn_date.year, t.txn_date.month), [0, 0, 0]
            )
            bucket_totals = monthly_buckets[(t.txn_date.year, t.txn_date.month)]
            bucket_totals[0] += t.credit_paise or 0
            bucket_totals[1] += t.debit_paise or 0
            bucket_totals[2] += 1

    running_cum_paise = 0
    for y, m in months_to_build:
        m_in, m_out, m_count = monthly_buckets.get((y, m), (0, 0, 0))
        m_net = m_in - m_out
        running_cum_paise += m_net
        m_label = date_type(y, m, 1).strftime("%b %Y")
        monthly_trend.append({
            "year": y,
            "month": m,
            "month_label": m_label,
            "inflows": round(m_in / 100.0, 2),
            "outflows": round(m_out / 100.0, 2),
            "net_flow": round(m_net / 100.0, 2),
            "transaction_count": m_count,
        })

    # 4. 30/60/90-Day Recurring Payment Forecast
    from app.treasury.recurring_detector import compute_30_60_90_forecast
    forecast_data = compute_30_60_90_forecast(
        group_closing_paise,
        all_active_txns,
        history_days,
        ref_date=last_txn or date_type.today(),
    )

    txns_in_period = sum(
        a["transaction_count"] for e in entity_results for a in e.get("accounts", [])
    )

    all_group_breaches = []
    for e in entity_results:
        all_group_breaches.extend(e.get("breach_details", []))

    overview = {
        "report_type": "Treasury Overview - Group Level",
        "data_range": {
            "first_transaction": first_txn.isoformat() if first_txn else None,
            "last_transaction": last_txn.isoformat() if last_txn else None,
            "transactions_in_period": txns_in_period,
            "history_days": history_days,
            "suggested_period": (
                None if not last_txn else (
                    treasury_periods.CURRENT_MONTH
                    if (last_txn.year, last_txn.month) == (date_type.today().year, date_type.today().month)
                    else treasury_periods.ALL_TIME
                )
            ),
            "suggested_start": first_txn.isoformat() if first_txn else None,
            "suggested_end": last_txn.isoformat() if last_txn else None,
        },
        "period": {
            "start_date": s_date.strftime("%Y-%m-%d") if s_date else None,
            "end_date": e_date.strftime("%Y-%m-%d") if e_date else None,
            **resolved.as_dict(),
        },
        "decision_cards": {
            "cash_runway": {
                "status": group_runway_status,
                "months": group_runway_months,
                "monthly_net_burn": round(group_monthly_burn_paise / 100.0, 2),
            },
            "net_cash_flow": {
                "current": round(group_net_flow_paise / 100.0, 2),
                "previous": round(prev_net_flow_paise / 100.0, 2),
                "delta": round(net_delta_paise / 100.0, 2),
                "delta_pct": net_delta_pct,
                "direction": net_delta_direction,
            },
            "liquidity_alerts": {
                "total_alerts": group_min_breaches + group_uncategorized_count,
                "min_balance_breaches": group_min_breaches,
                "uncategorized_count": group_uncategorized_count,
                "status": "warning" if (group_min_breaches + group_uncategorized_count) > 0 else "all_clear",
                "label": f"{group_min_breaches + group_uncategorized_count} alerts requiring action" if (group_min_breaches + group_uncategorized_count) > 0 else "All clear",
                "breach_details": all_group_breaches,
            },
            "overdue_receivables": {
                "available": False,
                "status": "Not enough data",
                "total_past_due": None,
                "oldest_overdue_days": None,
            }
        },
        "group_totals": {
            "opening_balance": round(group_opening_paise / 100.0, 2),
            "total_inflows": round(group_inflows_paise / 100.0, 2),
            "total_outflows": round(group_outflows_paise / 100.0, 2),
            "net_flow": round(group_net_flow_paise / 100.0, 2),
            "closing_balance": round(group_closing_paise / 100.0, 2),
            "unexplained_difference": round(
                (group_reported_closing_paise - group_closing_paise) / 100.0, 2),
            "balance_basis": ("derived" if group_bases == {"derived"}
                              else ("reported" if group_bases == {"reported"} else "mixed")),
            "min_balance_breaches": group_min_breaches,
            "uncategorized_count": group_uncategorized_count,
            "cash_runway": group_runway_status,
            "net_flow_pct_change": net_delta_pct,
            "inflow_categories": {k: round(v / 100.0, 2) for k, v in group_inflow_cats.items()},
            "outflow_categories": {k: round(v / 100.0, 2) for k, v in group_outflow_cats.items()}
        },
        "entities": entity_results,
        "monthly_trend": monthly_trend,
        "forecast": forecast_data,
    }

    cache.set(overview_key, overview, _TREASURY_TTL_SECONDS)
    return overview


@router.get("/treasury")
def get_treasury_report_endpoint(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    entity_id: Optional[str] = Query(None),
    account_id: Optional[str] = Query(None),
    period: Optional[str] = Query(
        None,
        description=("Named report period: current_month, last_month, "
                     "current_quarter, last_quarter, financial_year, "
                     "last_financial_year, all_time, or custom with explicit "
                     "start_date/end_date. Explicit dates always win."),
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Part 4: GET /reports/treasury — Treasury Overview Report API."""
    prd = period if isinstance(period, str) else None
    if prd and prd not in treasury_periods.PERIODS:
        raise HTTPException(
            status_code=400,
            detail=f"period must be one of {', '.join(treasury_periods.PERIODS)}",
        )
    return compute_treasury_overview(
        db, current_user.id,
        start_date=start_date if isinstance(start_date, str) else None,
        end_date=end_date if isinstance(end_date, str) else None,
        entity_id=entity_id if isinstance(entity_id, str) else None,
        account_id=account_id if isinstance(account_id, str) else None,
        period=prd,
    )


@router.get("/export/treasury")
def export_treasury_csv(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    entity_id: Optional[str] = Query(None),
    account_id: Optional[str] = Query(None),
    period: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Part 18: GET /reports/export/treasury — Treasury Overview CSV Export."""
    data = compute_treasury_overview(
        db, current_user.id,
        start_date=start_date if isinstance(start_date, str) else None,
        end_date=end_date if isinstance(end_date, str) else None,
        entity_id=entity_id if isinstance(entity_id, str) else None,
        account_id=account_id if isinstance(account_id, str) else None,
        period=period if isinstance(period, str) else None,
    )

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(["TREASURY OVERVIEW - GROUP LEVEL"])
    writer.writerow(["Period", f"{data['period']['start_date'] or 'All-Time'} to {data['period']['end_date'] or 'All-Time'}"])
    writer.writerow([])

    gt = data["group_totals"]
    entities = data["entities"]

    writer.writerow(["Metric", "Group Total", "% of Group"] + [_csv_safe(e["entity_name"]) for e in entities])
    writer.writerow(["Opening Balance", f"{gt['opening_balance']:.2f}", "-"] + [f"{e['opening_balance']:.2f}" for e in entities])
    writer.writerow(["Total Inflows", f"{gt['total_inflows']:.2f}", "-"] + [f"{e['total_inflows']:.2f}" for e in entities])
    writer.writerow(["Total Outflows", f"{gt['total_outflows']:.2f}", "-"] + [f"{e['total_outflows']:.2f}" for e in entities])
    writer.writerow(["Net Flow", f"{gt['net_flow']:.2f}", "-"] + [f"{e['net_flow']:.2f}" for e in entities])
    writer.writerow(["Closing Balance", f"{gt['closing_balance']:.2f}", "100.0%"] + [f"{e['closing_balance']:.2f}" for e in entities])
    writer.writerow(["% of Group Cash", "100.0%", "-"] + [f"{e.get('pct_of_group_cash', 0.0)}%" for e in entities])
    writer.writerow(["Cash Runway", gt.get("cash_runway", "-"), "-"] + [str(e.get("cash_runway", "-")) for e in entities])
    writer.writerow(["Net CF % Change (vs Prior)", f"{gt.get('net_flow_pct_change', '-')}%" if gt.get('net_flow_pct_change') is not None else "-", "-"] + [f"{e.get('net_flow_pct_change', '-')}%" if e.get('net_flow_pct_change') is not None else "-" for e in entities])
    writer.writerow(["Days of Cash on Hand", "-", "-"] + [str(e.get("days_of_cash", "-")) for e in entities])
    writer.writerow(["Min Balance Breaches", gt["min_balance_breaches"], "-"] + [e["min_balance_breaches"] for e in entities])
    writer.writerow(["Uncategorized Count", gt["uncategorized_count"], "-"] + [e["uncategorized_count"] for e in entities])

    any_accounts = any(e.get("accounts") for e in entities)
    if any_accounts:
        writer.writerow([])
        writer.writerow(["ACCOUNT DETAIL"])
        writer.writerow([
            "Entity", "Account", "Bank", "Currency", "Opening", "Inflows",
            "Outflows", "Net Flow", "Closing", "% of Entity", "% of Group",
            "Reported Closing", "Min Required", "Lowest Balance", "Breaches",
            "Uncategorized", "Transactions",
        ])
        for e in entities:
            for a in e.get("accounts", []):
                writer.writerow([
                    _csv_safe(e["entity_name"]), _csv_safe(a["account_label"]),
                    _csv_safe(a["bank_code"]), a["currency"],
                    f"{a['opening_balance']:.2f}", f"{a['total_inflows']:.2f}",
                    f"{a['total_outflows']:.2f}", f"{a['net_flow']:.2f}",
                    f"{a['closing_balance']:.2f}",
                    f"{a.get('pct_of_entity_cash', 0.0)}%",
                    f"{a.get('pct_of_group_cash', 0.0)}%",
                    "" if a["reported_closing_balance"] is None else f"{a['reported_closing_balance']:.2f}",
                    "" if a["min_balance_required"] is None else f"{a['min_balance_required']:.2f}",
                    "" if a["lowest_balance"] is None else f"{a['lowest_balance']:.2f}",
                    a["min_balance_breaches"], a["uncategorized_count"],
                    a["transaction_count"],
                ])

    # Add Projected Balance section to CSV export
    fc = data.get("forecast", {})
    if fc.get("available") and fc.get("horizons"):
        writer.writerow([])
        writer.writerow(["30 / 60 / 90-DAY PROJECTED BALANCE (ESTIMATED BASED ON RECURRING PATTERNS)"])
        writer.writerow(["Horizon", "Conservative (High-Confidence)", "Expected (High + Medium)"])
        for h in [30, 60, 90]:
            h_data = fc["horizons"].get(f"day_{h}", {})
            writer.writerow([f"+{h} Days", f"{h_data.get('conservative', 0.0):.2f}", f"{h_data.get('expected', 0.0):.2f}"])

    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=treasury_overview_report.csv"}
    )

