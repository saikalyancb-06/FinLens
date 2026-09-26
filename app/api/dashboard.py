import logging
from bisect import bisect_right
from typing import List, Optional
from uuid import UUID
from datetime import datetime, date as date_type, timedelta
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from app.database.session import get_db
from app.services.cache import cache
from app.models.user import User
from app.models.transaction import Transaction, SourceType
from app.models.uploaded_file import UploadedFile
from app.models.statement import Statement
from app.utils.security import get_current_user
from app.api.scoping import entity_scope
from app.api.dashboard_schemas import (
    ProcessedTransactionResponse,
    DashboardSummaryResponse,
    FilterMetadataResponse,
    MonthlySummaryItem,
    CategoryBreakdownItem,
    CashFlowItem,
    AgeingBucket,
    UnreconciledAgeingResponse,
    ForecastBacktestPoint,
    ForecastBacktestResponse,
    CashPositionPoint,
    CashPositionResponse,
    OutflowBucket,
    OutflowBreakdownResponse,
    CounterpartyRow,
    CounterpartyOutflowsResponse,
    EvidenceSource,
    ClassificationHealthResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Dashboard & Analytics"])

# ---------------------------------------------------------------------------
# Internal helper — base query for active (non-superseded) canonical records
# ---------------------------------------------------------------------------

def _active_txns(db: Session, user_id):
    """Return a base query for active (non-superseded) canonical transactions."""
    return db.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.superseded_by_id == None,
    )


# ---------------------------------------------------------------------------
# Read-through caching for the analytics panels
# ---------------------------------------------------------------------------
#
# One dashboard page load fires most of the endpoints below at once, and a user
# comparing two filters flips back and forth over the same handful of argument
# sets. These are pure reads over data that only changes on an import, an edit
# or a clear — all of which drop the user's entries.
#
# The TTL is the backstop, not the mechanism, so it is deliberately short: if an
# invalidation is ever missed, a wrong figure survives for seconds rather than
# until the next write.
_ANALYTICS_TTL_SECONDS = 30


def _cached_list(key: str, model_cls):
    """A cached list response rebuilt into models, or None if not cached.

    `mode="json"` on the way in matters: it lowers dates and UUIDs to the same
    strings the response would have carried anyway, so what is stored is what
    the endpoint emits, and the model constructor coerces them straight back.
    """
    hit = cache.get(key)
    return None if hit is None else [model_cls(**row) for row in hit]


def _store_list(key: str, items) -> None:
    cache.set(key, [item.model_dump(mode="json") for item in items],
              _ANALYTICS_TTL_SECONDS)


@router.get("/dashboard/summary", response_model=DashboardSummaryResponse)
def get_dashboard_summary(
    from_date: Optional[date_type] = Query(None, description="Start date (YYYY-MM-DD)."),
    to_date: Optional[date_type] = Query(None, description="End date (YYYY-MM-DD)."),
    entity_id: Optional[UUID] = Query(None),
    bank_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Dashboard summary metrics from canonical transactions table with date boundary and entity/bank filtering."""
    from app.models.category import Category

    # Every argument that narrows the population is in the key. A key that
    # omitted one would hand the figures for one date range or one account to a
    # request asking about another — a wrong number on a treasury screen, which
    # is worse than a slow one.
    summary_key = cache.user_key(current_user.id, "dashboard-summary",
                                 from_date, to_date, entity_id, bank_id)
    cached_summary = cache.get(summary_key)
    if cached_summary is not None:
        return DashboardSummaryResponse(**cached_summary)

    base_q = _active_txns(db, current_user.id)
    if entity_id:
        base_q = base_q.filter(entity_scope(entity_id))
    if bank_id:
        from app.models.account import Account
        acc_by_id = db.query(Account).filter(Account.id == bank_id, Account.user_id == current_user.id).first()
        if acc_by_id:
            base_q = base_q.filter(Transaction.account_id == acc_by_id.id)
        else:
            bank_accs = db.query(Account).filter(Account.user_id == current_user.id, Account.bank_id == bank_id).all()
            bank_acc_ids = [a.id for a in bank_accs]
            base_q = base_q.filter(Transaction.account_id.in_(bank_acc_ids)) if bank_acc_ids else base_q.filter(Transaction.account_id == bank_id)

    today = date_type.today()
    if from_date is not None or to_date is not None:
        eff_from = from_date or date_type(1970, 1, 1)
        eff_to = to_date or today
        target_q = base_q.filter(Transaction.txn_date >= eff_from, Transaction.txn_date <= eff_to)
        total_txns = target_q.count()
    else:
        target_q = base_q
        total_txns = base_q.count()

    # PostgreSQL's SUM(bigint) returns numeric, which SQLAlchemy hands back as a
    # Decimal — mixing that with the float arithmetic below raises TypeError.
    # Money is stored as integer paise, so int() is exact, not a rounding step.
    mtd_debit_paise = int(target_q.with_entities(func.coalesce(func.sum(Transaction.debit_paise), 0)).scalar() or 0)
    mtd_credit_paise = int(target_q.with_entities(func.coalesce(func.sum(Transaction.credit_paise), 0)).scalar() or 0)

    total_files = db.query(UploadedFile).filter(
        UploadedFile.user_id == current_user.id,
        UploadedFile.status == "COMPLETED"
    ).count()

    # Uncategorized = no category_id OR category linked to 'Uncategorized'
    uncat_category = db.query(Category).filter(Category.name.ilike("Uncategorized")).first()
    uncat_cat_id = uncat_category.id if uncat_category else None

    uncategorized_count = base_q.filter(
        (Transaction.category_id == None) | (Transaction.category_id == uncat_cat_id)
    ).count()

    # Convert paise → rupees at response boundary only
    total_debit_f  = round((mtd_debit_paise or 0) / 100.0, 2)
    total_credit_f = round((mtd_credit_paise or 0) / 100.0, 2)
    net_cash_flow_f = round(total_credit_f - total_debit_f, 2)

    # Calculate distinct entity and account counts based on the active FILTER SCOPE (master data scope)
    # rather than merely counting entities/accounts that have transactions in the current filtered date window.
    from app.models.account import Account
    from app.models.entity import Entity

    user_entities = db.query(Entity).filter(Entity.user_id == current_user.id).all()
    user_accounts = db.query(Account).filter(
        Account.user_id == current_user.id,
        Account.deleted_at == None
    ).all()

    if entity_id and bank_id:
        # Both entity and bank account specified
        matched_accs = [a for a in user_accounts if a.id == bank_id or a.bank_id == bank_id]
        if matched_accs:
            accounts_cnt = len(matched_accs)
            entities_cnt = len({a.entity_id for a in matched_accs if a.entity_id == entity_id}) or 1
        else:
            accounts_cnt = 1
            entities_cnt = 1
    elif entity_id:
        # Specific entity selected
        entities_cnt = 1
        accounts_cnt = len([a for a in user_accounts if a.entity_id == entity_id])
    elif bank_id:
        # Specific bank account selected
        matched_accs = [a for a in user_accounts if a.id == bank_id or a.bank_id == bank_id]
        if matched_accs:
            accounts_cnt = len(matched_accs)
            ent_ids = {a.entity_id for a in matched_accs if a.entity_id is not None}
            entities_cnt = len(ent_ids) if ent_ids else 1
        else:
            accounts_cnt = 1
            entities_cnt = 1
    else:
        # No entity or bank filter: full available master data scope
        entities_cnt = len(user_entities)
        accounts_cnt = len(user_accounts)

    all_debits = int(base_q.with_entities(func.coalesce(func.sum(Transaction.debit_paise), 0)).scalar() or 0)
    all_credits = int(base_q.with_entities(func.coalesce(func.sum(Transaction.credit_paise), 0)).scalar() or 0)

    # Closing position is the bank balance you actually hold: the latest running
    # balance on each account, summed. It is NOT credits minus debits — that is
    # only the net movement over the filtered window and ignores the opening
    # balance entirely, so under any date filter the two diverge completely.
    #
    # This walked every balance-bearing row into a dict and let the last write
    # per account win. DISTINCT ON asks the database for that same last row
    # directly: one row per account instead of one per transaction. The ORDER BY
    # is the old one reversed — txn_date DESC and row_index DESC NULLS FIRST is
    # exactly "the row the ascending pass would have finished on", including the
    # NULLS LAST/FIRST flip, so the row selected cannot differ.
    latest_balances = {
        txn_account_id: bal
        for txn_account_id, bal in base_q.with_entities(
            Transaction.account_id, Transaction.balance_paise
        ).filter(Transaction.balance_paise.isnot(None)).distinct(
            Transaction.account_id
        ).order_by(
            Transaction.account_id,
            Transaction.txn_date.desc(),
            Transaction.row_index.desc().nulls_first(),
        ).all()
    }

    if latest_balances:
        liquidity_paise = sum(latest_balances.values())
    else:
        # No running balances parsed (some statement formats omit them). Net
        # movement is the only figure available; it is a fallback, not the same
        # thing, so it is worth knowing that is what you are looking at.
        liquidity_paise = all_credits - all_debits
    liquidity_f = round(liquidity_paise / 100.0, 2)
    net_movement_f = round((all_credits - all_debits) / 100.0, 2)

    # Calculate Risk Alerts & Anomalies dynamically (e.g. large high-value txns > 50,000 or uncategorized)
    high_value_cnt = base_q.filter(
        (Transaction.debit_paise >= 5000000) | (Transaction.credit_paise >= 5000000)
    ).count()

    risk_alerts_cnt = high_value_cnt

    # ---- Real anomaly + compliance figures ---------------------------------
    # These used to be `uncategorized_count` and a hardcoded 100.0. Both now come
    # from app/compliance/, so the tiles move when something real changes.
    #
    # The account ids the filter bar has narrowed to, or None for "everything".
    # Findings are stored per account, so this is what lets them be counted on
    # the same population as the charts beside them.
    scoped_account_ids = None
    if bank_id:
        matched = [a.id for a in user_accounts if str(a.id) == str(bank_id) or (a.bank_id and str(a.bank_id) == str(bank_id))]
        scoped_account_ids = matched
    elif entity_id:
        scoped_account_ids = [a.id for a in user_accounts if a.entity_id and str(a.entity_id) == str(entity_id)]

    anomalies_cnt = 0
    critical_anomalies = 0
    resolved_anomalies_cnt = 0
    open_violations = 0
    critical_violations = 0
    compliance_pct = None
    never_scanned = True

    try:
        from app.models.compliance import AnomalyFinding, PolicyViolation
        from app.models.compliance import PolicyRule as _PolicyRule
        from app.compliance.policy_engine import compliance_summary
        from app.compliance.rules_seed import seed_policy_rules
        from app.compliance.auto_scan import run_scan_if_data_changed

        if not db.query(_PolicyRule).filter(_PolicyRule.user_id == current_user.id).first():
            seed_policy_rules(db, current_user.id)

        # Counts stay accurate and fresh, but a re-scan now costs something only
        # when the ledger, the rules or the findings have actually moved. This
        # was `run_scan_safely` unconditionally, which re-derived every anomaly
        # over every transaction on each GET to arrive at the same numbers.
        #
        # Still the same filtered scope as before, so what does and does not
        # trigger a scan is unchanged; `.with_entities(Transaction.id)` only
        # stops it hydrating a whole ORM transaction to answer "is there one?".
        has_txns = base_q.with_entities(Transaction.id).first() is not None
        if has_txns:
            run_scan_if_data_changed(db, current_user.id)

        # SCOPED THE SAME WAY THE REST OF THE PAGE IS.
        def _scope_findings(q, model):
            if scoped_account_ids is not None:
                q = q.filter(
                    or_(
                        model.account_id.in_(scoped_account_ids),
                        model.account_id.is_(None),
                    )
                ) if scoped_account_ids else q.filter(model.account_id.is_(None))
            if from_date:
                q = q.filter(or_(model.occurred_on.is_(None),
                                 model.occurred_on >= from_date))
            if to_date:
                q = q.filter(or_(model.occurred_on.is_(None),
                                 model.occurred_on <= to_date))
            return q

        anomaly_rows = dict(
            _scope_findings(
                db.query(AnomalyFinding.severity, func.count())
                .filter(AnomalyFinding.user_id == current_user.id,
                        AnomalyFinding.status == "open"),
                AnomalyFinding,
            ).group_by(AnomalyFinding.severity).all()
        )
        anomalies_cnt = sum(anomaly_rows.values())
        critical_anomalies = anomaly_rows.get("critical", 0)
        resolved_anomalies_cnt = _scope_findings(
            db.query(AnomalyFinding).filter(
                AnomalyFinding.user_id == current_user.id,
                AnomalyFinding.status.in_(("resolved", "false_positive")),
            ),
            AnomalyFinding,
        ).count()

        violation_rows = dict(
            _scope_findings(
                db.query(PolicyViolation.severity, func.count())
                .filter(PolicyViolation.user_id == current_user.id,
                        PolicyViolation.status == "open"),
                PolicyViolation,
            )
            .group_by(PolicyViolation.severity).all()
        )
        open_violations = sum(violation_rows.values())
        critical_violations = violation_rows.get("critical", 0)

        comp = compliance_summary(db, current_user.id)
        compliance_pct = comp.get("compliance_pct")
        never_scanned = bool(comp.get("never_scanned"))
    except Exception as exc:
        # Roll the session back: a failed statement poisons it, and every query
        # after this point would fail too if it were left in that state.
        db.rollback()
        logger.warning(
            "[Dashboard] compliance figures unavailable (%s: %s). "
            "Serving the dashboard without them — run `alembic upgrade head` if a "
            "column is missing.", exc.__class__.__name__, exc,
        )

    # ---- Reconciliation coverage -------------------------------------------
    unreconciled_count = 0
    unreconciled_value_paise = 0
    coverage_pct = None
    try:
        from app.models.reconciliation import ReconciliationItem, ReconciliationRun

        # Only the newest non-archived run per account counts. Reconciliation runs
        # supersede one another, so summing items across every historical run
        # counted the same bridge item once per re-run.
        runs = db.query(ReconciliationRun).filter(
            ReconciliationRun.user_id == current_user.id,
            ReconciliationRun.archived_at.is_(None),
        ).order_by(ReconciliationRun.account_id,
                   ReconciliationRun.created_at.asc()).all()
        latest_run_per_account = {r.account_id: r for r in runs}   # last write wins
        live_run_ids = [r.id for r in latest_run_per_account.values()]

        if live_run_ids:
            items = db.query(ReconciliationItem).filter(
                ReconciliationItem.user_id == current_user.id,
                ReconciliationItem.run_id.in_(live_run_ids),
            ).all()
            unreconciled_count = len(items)
            unreconciled_value_paise = sum(abs(i.amount_paise or 0) for i in items)

            # Coverage is matched value over total value reconciled in those runs,
            # not over transaction throughput. With no run at all it stays None
            # rather than 100%, because nothing has been reconciled — reporting a
            # perfect score for work never done is worse than reporting nothing.
            gross = all_debits + all_credits
            if gross:
                coverage_pct = round(100.0 * max(0, gross - unreconciled_value_paise) / gross, 1)
    except Exception:
        db.rollback()

    # ---- Bank charges: money the bank took, often recoverable ---------------
    charges_paise = 0
    try:
        charge_cat = db.query(Category).filter(Category.name.ilike("Bank Charges")).first()
        if charge_cat:
            charges_paise = int(base_q.filter(
                Transaction.category_id == charge_cat.id
            ).with_entities(func.coalesce(func.sum(Transaction.debit_paise), 0)).scalar() or 0)
    except Exception:
        pass

    # ---- Data freshness: how stale is what you are looking at ---------------
    last_stmt_date = None
    data_age_days = None
    try:
        latest = db.query(func.max(Transaction.txn_date)).filter(
            Transaction.user_id == current_user.id,
            Transaction.superseded_by_id.is_(None),
        ).scalar()
        if latest:
            last_stmt_date = latest.isoformat()
            data_age_days = (datetime.utcnow().date() - latest).days
    except Exception:
        pass

    # ---- Burn rate and runway ----------------------------------------------
    avg_daily_burn_f = 0.0
    runway_days = None
    try:
        span = db.query(func.min(Transaction.txn_date), func.max(Transaction.txn_date)).filter(
            Transaction.user_id == current_user.id,
            Transaction.superseded_by_id.is_(None),
        ).one()
        if span[0] and span[1]:
            days = max(1, (span[1] - span[0]).days)
            net_burn = all_debits - all_credits
            if net_burn > 0:
                avg_daily_burn_f = round(net_burn / days / 100.0, 2)
                if avg_daily_burn_f > 0 and liquidity_f > 0:
                    runway_days = int(liquidity_f / avg_daily_burn_f)
    except Exception:
        pass

    # Convert paise → rupees at response boundary only
    total_debit_f  = round((mtd_debit_paise or 0) / 100.0, 2)
    total_credit_f = round((mtd_credit_paise or 0) / 100.0, 2)

    summary = DashboardSummaryResponse(
        total_transactions=total_txns,
        total_debit=total_debit_f,
        total_credit=total_credit_f,
        net_cash_flow=round(total_credit_f - total_debit_f, 2),
        total_files_processed=total_files,
        uncategorized_count=uncategorized_count,
        uncategorized_txns=uncategorized_count,
        consolidated_liquidity=liquidity_f,
        risk_alerts=risk_alerts_cnt,
        anomalies=anomalies_cnt,
        resolved_anomalies=resolved_anomalies_cnt,
        pending_anomalies=anomalies_cnt,
        policy_compliance_pct=compliance_pct,
        entities_count=entities_cnt,
        accounts_count=accounts_cnt,
        critical_anomalies=critical_anomalies,
        open_violations=open_violations,
        critical_violations=critical_violations,
        unreconciled_count=unreconciled_count,
        unreconciled_value=round(unreconciled_value_paise / 100.0, 2),
        reconciliation_coverage_pct=coverage_pct,
        bank_charges=round(charges_paise / 100.0, 2),
        last_statement_date=last_stmt_date,
        data_age_days=data_age_days,
        avg_daily_burn=avg_daily_burn_f,
        runway_days=runway_days,
        net_movement=net_movement_f,
        compliance_never_scanned=never_scanned,
    )

    cache.set(summary_key, summary.model_dump(mode="json"), _ANALYTICS_TTL_SECONDS)
    return summary




@router.get("/analytics/filters", response_model=FilterMetadataResponse)
def get_transaction_filters(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Filter dropdown metadata for transactions workbench."""
    from app.models.category import Category
    from app.models.prediction import Prediction

    # Every category the transactions endpoint can actually FILTER on.
    #
    # This used to list only the names reachable through `category_id`, which
    # left out every row whose category came from a prediction — and
    # `/transactions?category=` matches on either. The dropdown built from this
    # therefore offered fewer options than the filter behind it accepted.
    #
    # The union is drawn in SQL because the alternative the workbench used
    # instead was to download the whole ledger and collect the distinct strings
    # in the browser: 1,823 rows and 3.5 MB to populate one <select>.
    linked = (
        db.query(Category.name.label("name"))
        .join(Transaction, Transaction.category_id == Category.id)
        .filter(
            Transaction.user_id == current_user.id,
            Transaction.superseded_by_id == None,
        )
        .distinct()
    )
    predicted = (
        db.query(Prediction.predicted_category.label("name"))
        .join(Transaction, Prediction.transaction_id == Transaction.id)
        .filter(
            Transaction.user_id == current_user.id,
            Transaction.superseded_by_id == None,
            Prediction.predicted_category.isnot(None),
        )
        .distinct()
    )
    cats = linked.union(predicted).all()

    min_date = db.query(func.min(Transaction.txn_date)).filter(
        Transaction.user_id == current_user.id,
        Transaction.superseded_by_id == None,
    ).scalar()

    max_date = db.query(func.max(Transaction.txn_date)).filter(
        Transaction.user_id == current_user.id,
        Transaction.superseded_by_id == None,
    ).scalar()

    cat_list = sorted({c[0] for c in cats if c[0]})
    type_list = ["debit", "credit"]

    min_date_str = min_date.strftime("%Y-%m-%d") if min_date else None
    max_date_str = max_date.strftime("%Y-%m-%d") if max_date else None

    from app.models.entity import Entity
    from app.models.account import Account

    user_entities = db.query(Entity).filter(Entity.user_id == current_user.id).all()
    entities_list = [{"id": str(e.id), "name": e.name} for e in user_entities]

    user_accounts = db.query(Account).filter(Account.user_id == current_user.id).all()
    accounts_list = [
        {
            "id": str(a.id),
            "account_number": a.account_number_masked or str(a.id),
            "bank_code": a.bank_code or "UNKNOWN",
            "entity_id": str(a.entity_id) if a.entity_id else None
        }
        for a in user_accounts
    ]

    return FilterMetadataResponse(
        categories=cat_list,
        transaction_types=type_list,
        min_date=min_date_str,
        max_date=max_date_str,
        entities=entities_list,
        accounts=accounts_list
    )


@router.get("/analytics/recent-transactions", response_model=List[ProcessedTransactionResponse])
def get_recent_transactions(
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    txns = (
        _active_txns(db, current_user.id)
        .order_by(Transaction.txn_date.desc(), Transaction.created_at.desc())
        .limit(limit)
        .all()
    )

    output = []
    for t in txns:
        deb  = (t.debit_paise or 0) / 100.0
        cred = (t.credit_paise or 0) / 100.0
        amt  = deb if deb > 0 else cred
        bal  = (t.balance_paise or 0) / 100.0

        cat_name = "Uncategorized"
        conf     = 0.95
        source   = t.source_channel or "STATEMENT"
        reasoning = "Canonical Transaction record"

        # The Prediction row is consulted even when a Category is attached. Using
        # `elif` here meant a transaction carrying BOTH — which is the normal case
        # after ingestion — never read its Prediction, so this endpoint reported a
        # fabricated 0.95 confidence and the ingestion channel in place of the
        # classifier. Same defect was fixed in app/api/transactions.py.
        if t.category_node:
            cat_name = t.category_node.name
        if t.prediction:
            cat_name  = cat_name if t.category_node else (t.prediction.predicted_category or "Uncategorized")
            conf      = t.prediction.confidence
            source    = "Rule Engine" if t.prediction.rule_used else "ML Model"
            reasoning = f"Rule: {t.prediction.rule_used}" if t.prediction.rule_used else "ML Prediction"


        output.append(ProcessedTransactionResponse(
            id=t.id,
            file_id=None,
            user_id=t.user_id,
            original_raw_text=t.narration_raw,
            date=datetime.combine(t.txn_date, datetime.min.time()) if t.txn_date else None,
            description=t.narration_clean or t.narration_raw,
            debit=deb,
            credit=cred,
            amount=amt,
            balance=bal,
            reference_number=t.reference_no,
            transaction_type=t.payment_method or t.direction.value,
            final_category=cat_name,
            confidence=conf,
            prediction_source=source,
            reasoning=reasoning,
            model_version="v1.0.0",
            processing_timestamp=t.created_at
        ))
    return output



# UNREACHABLE. app/api/transactions.py:334 registers DELETE /transactions/clear
# too, and main.py includes transactions_router (line 127) before dashboard_router
# (line 128), so Starlette always dispatches that one. This handler never runs —
# but because the OpenAPI dict is keyed by path and the later include overwrites
# it, the published spec advertises THIS operation, so documented behaviour and
# executed behaviour disagree. Kept (not deleted) to avoid changing the spec
# surface in the same pass as the data migration; delete it in a follow-up.
@router.delete("/transactions/clear", summary="Clear All Active Transactions for Current User",
               deprecated=True, include_in_schema=False)
def clear_user_transactions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Clears all processed transactions, uploaded files, statements and canonical transactions for the current user."""
    from app.models.processed_transaction import ProcessedTransaction
    from app.models.statement import Statement
    from app.models.prediction import Prediction
    from app.models.duplicate_match import DuplicateMatch
    from app.models.reconciliation import ReconciliationMatchLine, ReconciliationItem

    user_txn_ids = db.query(Transaction.id).filter(Transaction.user_id == current_user.id).subquery()

    # Delete predictions linked to user's transactions
    db.query(Prediction).filter(Prediction.transaction_id.in_(user_txn_ids)).delete(synchronize_session=False)

    # Delete duplicate match pairs for user
    db.query(DuplicateMatch).filter(DuplicateMatch.user_id == current_user.id).delete(synchronize_session=False)

    # Clear references in BRS and Reconciliation tables
    db.query(ReconciliationMatchLine).filter(ReconciliationMatchLine.bank_txn_id.in_(user_txn_ids)).delete(synchronize_session=False)
    db.query(ReconciliationItem).filter(ReconciliationItem.user_id == current_user.id).delete(synchronize_session=False)

    # 1. Delete processed transactions (ingestion staging layer)
    db.query(ProcessedTransaction).filter(
        ProcessedTransaction.user_id == current_user.id
    ).delete(synchronize_session=False)

    # 2. Delete canonical transactions
    db.query(Transaction).filter(
        Transaction.user_id == current_user.id
    ).delete(synchronize_session=False)

    # 3. Delete statements (also cascades transactions via FK)
    db.query(Statement).filter(
        Statement.user_id == current_user.id
    ).delete(synchronize_session=False)

    # 4. Delete uploaded file records (clears file dropdown)
    db.query(UploadedFile).filter(
        UploadedFile.user_id == current_user.id
    ).delete(synchronize_session=False)

    db.commit()

    # Every cached figure for this user was derived from rows that no longer
    # exist. Dropped after the commit, so a failed delete cannot evict a marker
    # that is still accurate.
    cache.invalidate_user(current_user.id)

    return {"status": "success", "message": "All outputs cleared — transactions, files, and statements deleted."}


@router.get("/analytics/monthly-summary", response_model=List[MonthlySummaryItem])
def get_monthly_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    monthly_key = cache.user_key(current_user.id, "analytics-monthly-summary")
    cached = _cached_list(monthly_key, MonthlySummaryItem)
    if cached is not None:
        return cached

    txns = _active_txns(db, current_user.id).all()

    summary_map: dict = {}
    for t in txns:
        m = t.txn_date.strftime("%Y-%m") if t.txn_date else "Unknown"
        if m not in summary_map:
            summary_map[m] = {"debit_paise": 0, "credit_paise": 0, "count": 0}
        summary_map[m]["debit_paise"]  += (t.debit_paise or 0)
        summary_map[m]["credit_paise"] += (t.credit_paise or 0)
        summary_map[m]["count"]        += 1

    output = []
    for m in sorted(summary_map.keys()):
        d_paise = summary_map[m]["debit_paise"]
        c_paise = summary_map[m]["credit_paise"]
        d = d_paise / 100.0
        c = c_paise / 100.0
        output.append(MonthlySummaryItem(
            month=m,
            total_debit=round(d, 2),
            total_credit=round(c, 2),
            net_cash_flow=round(c - d, 2),
            transaction_count=summary_map[m]["count"]
        ))

    _store_list(monthly_key, output)
    return output


@router.get("/analytics/category-breakdown", response_model=List[CategoryBreakdownItem])
def get_category_breakdown(
    start_date: Optional[date_type] = Query(None),
    end_date: Optional[date_type] = Query(None),
    entity_id: Optional[UUID] = Query(None),
    bank_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    breakdown_key = cache.user_key(current_user.id, "analytics-category-breakdown",
                                   start_date, end_date, entity_id, bank_id)
    cached = _cached_list(breakdown_key, CategoryBreakdownItem)
    if cached is not None:
        return cached

    query = _active_txns(db, current_user.id)
    if start_date:
        query = query.filter(Transaction.txn_date >= start_date)
    if end_date:
        query = query.filter(Transaction.txn_date <= end_date)
    if entity_id:
        query = query.filter(entity_scope(entity_id))
    if bank_id:
        from app.models.account import Account
        acc_by_id = db.query(Account).filter(Account.id == bank_id, Account.user_id == current_user.id).first()
        if acc_by_id:
            query = query.filter(Transaction.account_id == acc_by_id.id)
        else:
            bank_accs = db.query(Account).filter(Account.user_id == current_user.id, Account.bank_id == bank_id).all()
            bank_acc_ids = [a.id for a in bank_accs]
            query = query.filter(Transaction.account_id.in_(bank_acc_ids)) if bank_acc_ids else query.filter(Transaction.account_id == bank_id)

    txns = query.all()

    cat_map: dict = {}
    total_vol_paise = 0

    for t in txns:
        # Purpose leads: it answers "what was the money for", which is what a
        # breakdown chart is asking. The legacy category is the fallback for
        # rows not yet labelled, and it names the payment rail as often as the
        # purpose — 853 rows once sat under "NEFT Transfer" spanning 43
        # unrelated counterparties.
        # The tree's level 1 leads: it is the axis the drill-down uses, so a
        # breakdown built on anything else would disagree with what the user
        # sees one click later. `legacy_category` is the fallback for rows not
        # yet re-categorised, and the linked node for rows older than both.
        if t.category:
            cat = t.category
        elif t.legacy_category:
            cat = t.legacy_category
        elif t.category_node:
            cat = t.category_node.name
        elif t.prediction:
            cat = t.prediction.predicted_category or "Uncategorized"
        else:
            cat = "Uncategorized"

        deb_paise  = t.debit_paise or 0
        cred_paise = t.credit_paise or 0
        amt_paise  = deb_paise + cred_paise
        total_vol_paise += amt_paise

        if cat not in cat_map:
            cat_map[cat] = {"amount_paise": 0, "debit_paise": 0, "credit_paise": 0, "count": 0}
        cat_map[cat]["amount_paise"]  += amt_paise
        cat_map[cat]["debit_paise"]   += deb_paise
        cat_map[cat]["credit_paise"]  += cred_paise
        cat_map[cat]["count"]         += 1

    grand_total_paise = total_vol_paise if total_vol_paise > 0 else 1

    output = []
    for cat, data in cat_map.items():
        amt  = data["amount_paise"] / 100.0
        deb  = data["debit_paise"]  / 100.0
        cred = data["credit_paise"] / 100.0
        cat_type = "debit" if data["debit_paise"] >= data["credit_paise"] else "credit"
        output.append(CategoryBreakdownItem(
            category=cat,
            amount=round(amt, 2),
            percentage=round((data["amount_paise"] / grand_total_paise) * 100, 2),
            count=data["count"],
            type=cat_type
        ))

    _store_list(breakdown_key, output)
    return output


@router.get("/analytics/cash-flow", response_model=List[CashFlowItem])
def get_cash_flow(
    interval: str = Query("monthly", pattern="^(daily|monthly)$", description="Aggregation granularity: 'daily' or 'monthly'."),
    start_date: Optional[date_type] = Query(None),
    end_date: Optional[date_type] = Query(None),
    entity_id: Optional[UUID] = Query(None),
    bank_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Cash flow trend aggregation (Daily or Monthly) using integer paise arithmetic."""
    # `interval` is in the key alongside the filters: daily and monthly are two
    # different answers to the same question, and sharing an entry between them
    # would draw one chart with the other's buckets.
    cash_flow_key = cache.user_key(current_user.id, "analytics-cash-flow",
                                   interval, start_date, end_date, entity_id, bank_id)
    cached = _cached_list(cash_flow_key, CashFlowItem)
    if cached is not None:
        return cached

    query = _active_txns(db, current_user.id)
    if start_date:
        query = query.filter(Transaction.txn_date >= start_date)
    if end_date:
        query = query.filter(Transaction.txn_date <= end_date)
    if entity_id:
        query = query.filter(entity_scope(entity_id))
    if bank_id:
        from app.models.account import Account
        acc_by_id = db.query(Account).filter(Account.id == bank_id, Account.user_id == current_user.id).first()
        if acc_by_id:
            query = query.filter(Transaction.account_id == acc_by_id.id)
        else:
            bank_accs = db.query(Account).filter(Account.user_id == current_user.id, Account.bank_id == bank_id).all()
            bank_acc_ids = [a.id for a in bank_accs]
            query = query.filter(Transaction.account_id.in_(bank_acc_ids)) if bank_acc_ids else query.filter(Transaction.account_id == bank_id)

    txns = query.order_by(Transaction.txn_date.asc()).all()

    cf_map: dict = {}
    for t in txns:
        if not t.txn_date:
            continue
        if interval == "daily":
            period = t.txn_date.strftime("%Y-%m-%d")
        else:
            period = t.txn_date.strftime("%Y-%m")

        if period not in cf_map:
            cf_map[period] = {"inflow_paise": 0, "outflow_paise": 0}
        cf_map[period]["inflow_paise"]  += (t.credit_paise or 0)
        cf_map[period]["outflow_paise"] += (t.debit_paise or 0)

    output = []
    for period in sorted(cf_map.keys()):
        in_f  = cf_map[period]["inflow_paise"]  / 100.0
        out_f = cf_map[period]["outflow_paise"] / 100.0
        output.append(CashFlowItem(
            period=period,
            inflow=round(in_f, 2),
            outflow=round(out_f, 2),
            net=round(in_f - out_f, 2)
        ))

    _store_list(cash_flow_key, output)
    return output



# Ageing bands. 30-day steps with an open-ended tail is the convention every
# ageing schedule uses, and matching it means the numbers can be read straight
# across to a payables or receivables ageing without re-bucketing.
AGEING_BANDS = [
    ("0-30 days", 0, 30),
    ("31-60 days", 31, 60),
    ("61-90 days", 61, 90),
    ("90+ days", 91, None),
]


@router.get("/analytics/unreconciled-ageing", response_model=UnreconciledAgeingResponse)
def get_unreconciled_ageing(
    entity_id: Optional[UUID] = Query(None),
    bank_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Ageing of the items still sitting on the bank reconciliation bridge.

    This is the schedule that has to be cleared before a period closes: every
    unmatched bank line and every unmatched book entry, banded by how long it
    has been outstanding. A 90-day-old uncleared cheque is a different problem
    from one raised last week, and a single "unreconciled value" figure - which
    is all the KPI strip shows - cannot tell them apart.

    Both sides are reported separately because they mean opposite things. An
    aged BANK item is money the bank moved that the books have never recorded;
    an aged BOOK item is something the books expect that never reached the bank.
    Netting them into one number hides which of the two you actually have.

    Age is taken from `ReconciliationItem.age_days`, computed at run time as
    (period_to - transaction date). It therefore ages as at the reconciliation
    date rather than as at today, which is both the accounting convention and
    the reason this figure does not quietly change between page loads.
    """
    from app.models.reconciliation import ReconciliationItem, ReconciliationRun

    ageing_key = cache.user_key(current_user.id, "analytics-unreconciled-ageing",
                                entity_id, bank_id)
    cached_ageing = cache.get(ageing_key)
    if cached_ageing is not None:
        return UnreconciledAgeingResponse(**cached_ageing)

    empty = UnreconciledAgeingResponse(
        as_of=None,
        buckets=[AgeingBucket(label=lbl, min_days=lo, max_days=hi,
                              bank_count=0, bank_value=0.0,
                              book_count=0, book_value=0.0,
                              total_count=0, total_value=0.0, exceptions=0)
                 for lbl, lo, hi in AGEING_BANDS],
        total_count=0, total_value=0.0, exceptions=0,
        oldest_days=None, accounts_covered=0,
    )

    try:
        # Same scoping rule as the reconciliation KPI: only the newest
        # non-archived run per account. Summing every historical run counts the
        # same bridge item once per re-run and inflates the ageing.
        run_q = db.query(ReconciliationRun).filter(
            ReconciliationRun.user_id == current_user.id,
            ReconciliationRun.archived_at.is_(None),
        )
        if bank_id:
            from app.models.account import Account
            acc = db.query(Account).filter(
                Account.id == bank_id, Account.user_id == current_user.id).first()
            if acc:
                run_q = run_q.filter(ReconciliationRun.account_id == acc.id)
            else:
                bank_acc_ids = [
                    a.id for a in db.query(Account).filter(
                        Account.user_id == current_user.id,
                        Account.bank_id == bank_id).all()
                ]
                run_q = (run_q.filter(ReconciliationRun.account_id.in_(bank_acc_ids))
                         if bank_acc_ids else run_q.filter(False))

        runs = run_q.order_by(ReconciliationRun.account_id,
                              ReconciliationRun.created_at.asc()).all()
        latest_per_account = {r.account_id: r for r in runs}     # last write wins
        if not latest_per_account:
            return empty

        live = list(latest_per_account.values())
        items = db.query(ReconciliationItem).filter(
            ReconciliationItem.user_id == current_user.id,
            ReconciliationItem.run_id.in_([r.id for r in live]),
        ).all()
        if not items:
            return empty

        buckets = []
        grand_count = grand_value = grand_exceptions = 0
        oldest = 0

        for label, lo, hi in AGEING_BANDS:
            in_band = [
                i for i in items
                if (i.age_days or 0) >= lo and (hi is None or (i.age_days or 0) <= hi)
            ]
            bank = [i for i in in_band if str(getattr(i.side, "value", i.side)) == "bank"]
            book = [i for i in in_band if str(getattr(i.side, "value", i.side)) != "bank"]

            # abs(): a bridge item's sign encodes add-or-subtract against the
            # balance, not whether it is outstanding. Summing signed amounts
            # would let an addition cancel a subtraction and report a bucket as
            # empty while it still holds work.
            bank_value = sum(abs(i.amount_paise or 0) for i in bank)
            book_value = sum(abs(i.amount_paise or 0) for i in book)
            exceptions = sum(1 for i in in_band if i.exception_flag)

            buckets.append(AgeingBucket(
                label=label, min_days=lo, max_days=hi,
                bank_count=len(bank), bank_value=int(bank_value) / 100.0,
                book_count=len(book), book_value=int(book_value) / 100.0,
                total_count=len(in_band),
                total_value=int(bank_value + book_value) / 100.0,
                exceptions=exceptions,
            ))
            grand_count += len(in_band)
            grand_value += int(bank_value + book_value)
            grand_exceptions += exceptions

        for i in items:
            oldest = max(oldest, i.age_days or 0)

        ageing = UnreconciledAgeingResponse(
            as_of=max(r.period_to for r in live),
            buckets=buckets,
            total_count=grand_count,
            total_value=grand_value / 100.0,
            exceptions=grand_exceptions,
            oldest_days=oldest,
            accounts_covered=len(latest_per_account),
        )
        # Only the computed schedule is cached. The `empty` returns below and
        # above are "we could not answer", and caching those would keep serving
        # a blank panel for the whole TTL after the cause had cleared.
        cache.set(ageing_key, ageing.model_dump(mode="json"), _ANALYTICS_TTL_SECONDS)
        return ageing
    except Exception:
        # Consistent with the rest of this module: a dashboard panel must never
        # take the page down. An empty schedule is visibly empty; a 500 blanks
        # every chart beside it.
        db.rollback()
        logger.exception("[Dashboard] unreconciled ageing failed")
        return empty


# ---------------------------------------------------------------------------
# Forecast accuracy
# ---------------------------------------------------------------------------
#
# The application has never stored forecast snapshots — there is no table of
# "what we predicted last month" to read back. So rather than fabricate an
# accuracy figure, this replays the forecast the application already owns.
#
# `compute_30_60_90_forecast` is the engine behind the treasury report's
# 30/60/90 bands. Handing it only the transactions that existed as at a past
# date reproduces exactly what it would have said on that date, and the balance
# that actually materialised a horizon later is already in the statement data.
# Walking that pair forward over the history gives a real error distribution.
#
# The forecast is NOT re-derived here and no new formula is introduced: the
# scoring is 100 − MAPE, which is the standard measure, and the projection
# itself is the application's own.

_BACKTEST_MIN_HISTORY_DAYS = 90     # the engine's own floor is 30; 90 gives it
                                    # a chance to see a monthly cadence repeat
_BACKTEST_STEP_DAYS = 30            # one cut-off per month of history


def _scope_txn_query(db: Session, user_id, entity_id, bank_id):
    """Apply the same entity/bank narrowing every other panel on the page uses.

    `bank_id` is an account id when it comes from the dashboard's account
    picker, and a bank id when it comes from a bank-level filter. Both are
    accepted, exactly as `get_dashboard_summary` accepts them, so one filter
    bar cannot mean two different populations on two charts.
    """
    q = _active_txns(db, user_id)
    if entity_id:
        q = q.filter(entity_scope(entity_id))
    if bank_id:
        from app.models.account import Account
        acc_by_id = db.query(Account).filter(
            Account.id == bank_id, Account.user_id == user_id).first()
        if acc_by_id:
            q = q.filter(Transaction.account_id == acc_by_id.id)
        else:
            bank_acc_ids = [
                a.id for a in db.query(Account).filter(
                    Account.user_id == user_id, Account.bank_id == bank_id).all()
            ]
            q = (q.filter(Transaction.account_id.in_(bank_acc_ids))
                 if bank_acc_ids else q.filter(Transaction.account_id == bank_id))
    return q


@router.get("/analytics/forecast-backtest", response_model=ForecastBacktestResponse)
def get_forecast_backtest(
    horizon_days: int = Query(30, ge=7, le=90,
                              description="Forecast horizon to score: 30, 60 or 90."),
    start_date: Optional[date_type] = Query(None),
    end_date: Optional[date_type] = Query(None),
    entity_id: Optional[UUID] = Query(None),
    bank_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Forecast vs actual, reconstructed by replaying the app's own forecast engine.

    See `ForecastBacktestResponse` for why this is a replay rather than a read
    of stored predictions.
    """
    from app.treasury.recurring_detector import compute_30_60_90_forecast

    # The engine exposes 30/60/90 bands only, so the requested horizon snaps to
    # the nearest one it can actually answer for.
    horizon = min((30, 60, 90), key=lambda h: abs(h - horizon_days))

    # Keyed on the SNAPPED horizon, not the raw query value: 28 and 32 both mean
    # 30 here, and keying on the raw number would compute the same answer three
    # times over and cache it three times.
    backtest_key = cache.user_key(current_user.id, "analytics-forecast-backtest",
                                  horizon, start_date, end_date, entity_id, bank_id)
    cached_backtest = cache.get(backtest_key)
    if cached_backtest is not None:
        return ForecastBacktestResponse(**cached_backtest)

    unavailable = lambda why: ForecastBacktestResponse(
        available=False, reason=why, horizon_days=horizon,
        points=[], points_count=0,
    )

    try:
        q = _scope_txn_query(db, current_user.id, entity_id, bank_id)
        if start_date:
            q = q.filter(Transaction.txn_date >= start_date)
        if end_date:
            q = q.filter(Transaction.txn_date <= end_date)

        txns = [t for t in q.order_by(Transaction.txn_date.asc(),
                                      Transaction.row_index.asc().nulls_last()).all()
                if t.txn_date]
        if not txns:
            return unavailable("No transactions in the selected period")

        first, last = txns[0].txn_date, txns[-1].txn_date
        span_days = (last - first).days
        needed = _BACKTEST_MIN_HISTORY_DAYS + horizon
        if span_days < needed:
            return unavailable(
                f"Needs at least {needed} days of history to score a "
                f"{horizon}-day forecast; the selected period holds {span_days}"
            )

        # Balance as at a date = the last running balance seen on each account
        # on or before it, summed. Same rule as the closing position on the KPI
        # strip, so the two cannot disagree.
        #
        # `txns` is already sorted by (txn_date, row_index), so one forward pass
        # produces that figure for every date at once: overwriting per account as
        # it goes is precisely "the last balance seen on or before here". This
        # used to re-scan the full list per call, twice per cut-off — an O(n)
        # walk inside an O(k) loop, for a value that only ever moves forward.
        txn_dates = [t.txn_date for t in txns]
        balance_dates: List[date_type] = []
        balance_prefix: List[int] = []
        _per_account: dict = {}
        for _t in txns:
            if _t.balance_paise is not None:
                _per_account[_t.account_id] = _t.balance_paise
            if balance_dates and balance_dates[-1] == _t.txn_date:
                balance_prefix[-1] = sum(_per_account.values())
            else:
                balance_dates.append(_t.txn_date)
                balance_prefix.append(sum(_per_account.values()))

        def balance_as_of(cut) -> int:
            # Rightmost entry at or before `cut`; nothing yet means no balances
            # have been seen, which is the empty-dict sum the walk started from.
            idx = bisect_right(balance_dates, cut)
            return balance_prefix[idx - 1] if idx else 0

        points = []
        cut = first + timedelta(days=_BACKTEST_MIN_HISTORY_DAYS)
        while cut <= last - timedelta(days=horizon):
            # Same prefix the filter produced, taken as a slice: the list is in
            # ascending date order, so every row at or before `cut` is a prefix.
            history = txns[:bisect_right(txn_dates, cut)]
            baseline_paise = balance_as_of(cut)

            fc = compute_30_60_90_forecast(
                baseline_paise, history, (cut - first).days, ref_date=cut,
            )
            if fc.get("available"):
                band = fc["horizons"][f"day_{horizon}"]
                actual_paise = balance_as_of(cut + timedelta(days=horizon))
                actual = round(actual_paise / 100.0, 2)
                err = (abs(band["expected"] - actual) / abs(actual) * 100.0
                       if actual else None)
                points.append(ForecastBacktestPoint(
                    as_of=cut,
                    forecast_date=cut + timedelta(days=horizon),
                    baseline=round(baseline_paise / 100.0, 2),
                    forecast_expected=band["expected"],
                    forecast_conservative=band["conservative"],
                    actual=actual,
                    error_pct=round(err, 1) if err is not None else None,
                ))
            cut += timedelta(days=_BACKTEST_STEP_DAYS)

        # One point is an anecdote, not an accuracy. Two is the floor.
        if len(points) < 2:
            return unavailable(
                "Not enough comparable periods to measure forecast accuracy"
            )

        def score(get_forecast) -> Optional[float]:
            errs = []
            for p in points:
                if not p.actual:
                    continue
                # Capped at 1.0 so a single wild miss cannot drag the mean below
                # zero and turn a percentage into a negative number.
                errs.append(min(abs(get_forecast(p) - p.actual) / abs(p.actual), 1.0))
            if not errs:
                return None
            return round(max(0.0, 100.0 * (1 - sum(errs) / len(errs))), 1)

        backtest = ForecastBacktestResponse(
            available=True,
            horizon_days=horizon,
            method=(f"Walk-forward backtest of the recurring-series forecast at "
                    f"{horizon}-day horizon; accuracy = 100 - MAPE"),
            accuracy_pct=score(lambda p: p.forecast_expected),
            accuracy_conservative_pct=score(lambda p: p.forecast_conservative),
            points=points,
            points_count=len(points),
        )
        # As with the ageing panel, only a real result is cached — the
        # `unavailable(...)` returns describe a condition that can clear.
        cache.set(backtest_key, backtest.model_dump(mode="json"),
                  _ANALYTICS_TTL_SECONDS)
        return backtest
    except Exception:
        db.rollback()
        logger.exception("[Dashboard] forecast backtest failed")
        return unavailable("Forecast comparison could not be computed")


@router.get("/analytics/cash-position", response_model=CashPositionResponse)
def get_cash_position(
    start_date: Optional[date_type] = Query(None),
    end_date: Optional[date_type] = Query(None),
    entity_id: Optional[UUID] = Query(None),
    bank_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The cash balance curve the liquidity chart is drawn from.

    Computed here rather than in the browser for one reason: which row is a
    day's *closing* balance depends on `row_index`, the position the line held
    on the statement, and the transactions endpoint neither orders by it nor
    returns it. Deriving the curve client-side from that response would pick an
    arbitrary row whenever a day held more than one movement.
    """
    from app.models.account import Account

    position_key = cache.user_key(current_user.id, "analytics-cash-position",
                                  start_date, end_date, entity_id, bank_id)
    cached_position = cache.get(position_key)
    if cached_position is not None:
        return CashPositionResponse(**cached_position)

    empty = CashPositionResponse(points=[], accounts_total=0)

    try:
        q = _scope_txn_query(db, current_user.id, entity_id, bank_id)
        if start_date:
            q = q.filter(Transaction.txn_date >= start_date)
        if end_date:
            q = q.filter(Transaction.txn_date <= end_date)

        rows = q.with_entities(
            Transaction.txn_date, Transaction.account_id, Transaction.balance_paise,
        ).filter(
            Transaction.balance_paise.isnot(None)
        ).order_by(
            Transaction.txn_date.asc(),
            Transaction.row_index.asc().nulls_last(),
        ).all()

        # ---- The accounts in scope, and the floor they are held to ----------
        acc_q = db.query(Account).filter(
            Account.user_id == current_user.id, Account.deleted_at == None)
        if entity_id:
            acc_q = acc_q.filter(Account.entity_id == entity_id)
        accounts = acc_q.all()
        if bank_id:
            accounts = [a for a in accounts
                        if str(a.id) == str(bank_id) or str(a.bank_id) == str(bank_id)]

        with_threshold = [a for a in accounts if a.min_balance_paise is not None]
        min_liquidity = (round(sum(a.min_balance_paise for a in with_threshold) / 100.0, 2)
                         if with_threshold else None)
        codes = {(a.currency or "INR") for a in accounts}

        if not rows:
            return CashPositionResponse(
                points=[], min_liquidity=min_liquidity,
                accounts_with_threshold=len(with_threshold),
                accounts_total=len(accounts),
                currency=(list(codes)[0] if len(codes) == 1 else None),
                currencies_mixed=len(codes) > 1,
            )

        # Last balance seen per account per day, then forward-filled and summed.
        per_day_per_account: dict = {}
        for txn_date, account_id, balance_paise in rows:
            per_day_per_account.setdefault(txn_date, {})[account_id] = balance_paise

        running: dict = {}
        points = []
        for day in sorted(per_day_per_account.keys()):
            running.update(per_day_per_account[day])
            points.append(CashPositionPoint(
                date=day, balance=round(sum(running.values()) / 100.0, 2)))

        position = CashPositionResponse(
            points=points,
            current_cash=points[-1].balance,
            as_of=points[-1].date,
            min_liquidity=min_liquidity,
            accounts_with_threshold=len(with_threshold),
            accounts_total=len(accounts),
            currency=(list(codes)[0] if len(codes) == 1 else None),
            currencies_mixed=len(codes) > 1,
        )
        cache.set(position_key, position.model_dump(mode="json"),
                  _ANALYTICS_TTL_SECONDS)
        return position
    except Exception:
        db.rollback()
        logger.exception("[Dashboard] cash position failed")
        return empty


# ---------------------------------------------------------------------------
# Outflow aggregates
# ---------------------------------------------------------------------------
#
# Both of these replace work the dashboard used to do in the browser over the
# full `/transactions?limit=10000` response. Measured in Chrome against the real
# dataset, that one request was 3.6 MB decoded and took 21 seconds, and while it
# serialised it held the GIL and starved every other request on the page — a
# 256-byte `/v1/bank-master/entities` took 16.7 seconds waiting behind it.
# Dropping it from the batch took the dashboard's API wall time from 2,076 ms to
# 368 ms on a warm server.
#
# All of that was to draw seventeen bars. These endpoints return a couple of
# kilobytes and reproduce the browser's grouping rules exactly, so the panels
# render identically.

def _outflow_rows(db, user_id, start_date, end_date, entity_id, bank_id, search):
    """Only the columns the two aggregates need, never whole ORM objects.

    `with_entities` is the point of this helper: hydrating 1,823 Transaction
    instances in order to read nine fields off each was a large part of what
    made the old request so expensive.
    """
    q = _scope_txn_query(db, user_id, entity_id, bank_id)
    if start_date:
        q = q.filter(Transaction.txn_date >= start_date)
    if end_date:
        q = q.filter(Transaction.txn_date <= end_date)
    if search:
        # The same two columns the transactions list searches, so a dashboard
        # narrowed by the search box agrees with the transactions page.
        pattern = "%" + search + "%"
        q = q.filter(
            Transaction.narration_clean.ilike(pattern)
            | Transaction.narration_raw.ilike(pattern)
        )
    return q.filter(Transaction.debit_paise > 0).with_entities(
        Transaction.debit_paise,
        Transaction.flow_type,
        Transaction.category_path,
        Transaction.category,
        Transaction.legacy_category,
        Transaction.counterparty,
        Transaction.merchant,
        Transaction.narration_clean,
        Transaction.narration_raw,
    ).all()


def _is_internal(flow_type, category_path) -> bool:
    """Own-account movement, judged on signals the application sets itself.

    Two of them, because neither alone is complete: the parser's `flow_type`
    catches rows the classifier never labelled, and the classifier's path
    catches rows the parser read as ordinary outflows.

    Note what this does NOT exclude. `Transfers > Person to Person` is a real
    payment to a real party and stays in — which is why the breakdown groups at
    the tree's second level. Grouping at level 1 would put those payments back
    under a single "Transfers" bar and undo the point of the exercise.
    """
    return (str(flow_type or "").upper() == "TRANSFER"
            or "own account transfer" in str(category_path or "").lower())


def _bucket_of(category_path, category, legacy_category) -> str:
    path = [p.strip() for p in str(category_path or "").split(">") if p.strip()]
    if len(path) >= 2:
        return path[1]
    if len(path) == 1:
        return path[0]
    return category or legacy_category or "Uncategorised"


@router.get("/analytics/outflow-breakdown", response_model=OutflowBreakdownResponse)
def get_outflow_breakdown(
    top: int = Query(9, ge=1, le=50),
    start_date: Optional[date_type] = Query(None),
    end_date: Optional[date_type] = Query(None),
    entity_id: Optional[UUID] = Query(None),
    bank_id: Optional[UUID] = Query(None),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Where the money actually went, internal transfers excluded."""
    key = cache.user_key(current_user.id, "analytics-outflow-breakdown",
                         top, start_date, end_date, entity_id, bank_id, search)
    hit = cache.get(key)
    if hit is not None:
        return OutflowBreakdownResponse(**hit)

    totals: dict = {}
    total = excluded_value = excluded_count = 0

    for (debit, flow_type, category_path, category, legacy_category,
         _cp, _merchant, _clean, _raw) in _outflow_rows(
            db, current_user.id, start_date, end_date, entity_id, bank_id, search):
        debit = int(debit or 0)
        if debit <= 0:
            continue
        if _is_internal(flow_type, category_path):
            excluded_value += debit
            excluded_count += 1
            continue
        label = _bucket_of(category_path, category, legacy_category)
        entry = totals.setdefault(label, {"value": 0, "count": 0})
        entry["value"] += debit
        entry["count"] += 1
        total += debit

    if not total:
        out = OutflowBreakdownResponse(
            available=False,
            excluded_value=round(excluded_value / 100.0, 2),
            excluded_count=excluded_count,
        )
        cache.set(key, out.model_dump(mode="json"), _ANALYTICS_TTL_SECONDS)
        return out

    ranked = sorted(totals.items(), key=lambda kv: kv[1]["value"], reverse=True)
    head, tail = ranked[:top], ranked[top:]
    rows = [
        OutflowBucket(label=label, value=round(v["value"] / 100.0, 2),
                      count=v["count"], share=round(v["value"] / total * 100, 4))
        for label, v in head
    ]
    if tail:
        # Aggregated rather than truncated: the bars below the cut are too thin
        # to read, but the total still has to be the real total.
        tail_value = sum(v["value"] for _, v in tail)
        rows.append(OutflowBucket(
            label="Other (%d)" % len(tail),
            value=round(tail_value / 100.0, 2),
            count=sum(v["count"] for _, v in tail),
            share=round(tail_value / total * 100, 4),
        ))

    out = OutflowBreakdownResponse(
        available=True, rows=rows, total=round(total / 100.0, 2),
        excluded_value=round(excluded_value / 100.0, 2),
        excluded_count=excluded_count, categories_found=len(ranked),
    )
    cache.set(key, out.model_dump(mode="json"), _ANALYTICS_TTL_SECONDS)
    return out


@router.get("/analytics/counterparty-outflows",
            response_model=CounterpartyOutflowsResponse)
def get_counterparty_outflows(
    top: int = Query(8, ge=1, le=50),
    start_date: Optional[date_type] = Query(None),
    end_date: Optional[date_type] = Query(None),
    entity_id: Optional[UUID] = Query(None),
    bank_id: Optional[UUID] = Query(None),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Who consumed the most cash.

    Same exclusions as the breakdown above, deliberately: the two panels sit
    side by side, and if one counted internal transfers and the other did not,
    their totals would disagree for reasons no reader could see.
    """
    key = cache.user_key(current_user.id, "analytics-counterparty-outflows",
                         top, start_date, end_date, entity_id, bank_id, search)
    hit = cache.get(key)
    if hit is not None:
        return CounterpartyOutflowsResponse(**hit)

    totals: dict = {}
    total = 0

    for (debit, flow_type, category_path, _cat, _legacy,
         counterparty, merchant, narration_clean, narration_raw) in _outflow_rows(
            db, current_user.id, start_date, end_date, entity_id, bank_id, search):
        debit = int(debit or 0)
        if debit <= 0 or _is_internal(flow_type, category_path):
            continue
        name = (counterparty or merchant or narration_clean or narration_raw
                or "Unidentified").strip() or "Unidentified"
        entry = totals.setdefault(name, {"value": 0, "count": 0})
        entry["value"] += debit
        entry["count"] += 1
        total += debit

    if not total:
        out = CounterpartyOutflowsResponse(available=False)
        cache.set(key, out.model_dump(mode="json"), _ANALYTICS_TTL_SECONDS)
        return out

    ranked = sorted(totals.items(), key=lambda kv: kv[1]["value"], reverse=True)
    rows = [
        CounterpartyRow(name=name, value=round(v["value"] / 100.0, 2),
                        count=v["count"], share=round(v["value"] / total * 100, 4))
        for name, v in ranked[:top]
    ]
    top5_value = sum(v["value"] for _, v in ranked[:5])

    out = CounterpartyOutflowsResponse(
        available=True, rows=rows, total=round(total / 100.0, 2),
        top5_share=round(top5_value / total * 100, 4),
        top5_count=min(5, len(ranked)), distinct=len(ranked),
    )
    cache.set(key, out.model_dump(mode="json"), _ANALYTICS_TTL_SECONDS)
    return out


# ---------------------------------------------------------------------------
# Classification health
# ---------------------------------------------------------------------------
#
# What this answers, in the user's words: "are all the transactions getting
# classified even though they are yet to be reviewed?"
#
# Yes — every row carries a category. But "carries a category" and "we know what
# this money was for" are different claims, and the dashboard was showing
# neither. This endpoint reports both, plus the EVIDENCE each answer rests on,
# because that is what makes the difference legible: a category derived from
# "NEFT" is not the same kind of fact as one derived from a merchant name, and
# a screen that presents them identically is misleading whichever way you read
# it.
#
# The signal ranking is the classifier's own. Amount does NOT invent a category
# — see `_apply_amount_signal` in app/categorization/rule_engine.py, which only
# nudges candidates that some textual signal already produced.

#: How each `rule_used` family is described to a human. Anything unrecognised
#: is passed through rather than hidden, so a new rule family shows up as
#: itself instead of silently vanishing into an "other" bucket.
_EVIDENCE_LABELS = {
    "merchant": "Known merchant",
    "narration_pattern": "Narration pattern",
    "phrase": "Narration phrase",
    "trade_name": "Trade / business name",
    "context": "Surrounding context",
    "keyword": "Keyword",
    "weak": "Payment rail only",
    "counterparty_memory": "Your saved decision",
    "": "No usable signal",
}

#: Families whose answer names how the money MOVED rather than what it was for.
#: These are the ones a person genuinely has to settle.
_RAIL_ONLY = {"weak", ""}


@router.get("/analytics/classification-health", response_model=ClassificationHealthResponse)
def get_classification_health(
    start_date: Optional[date_type] = Query(None),
    end_date: Optional[date_type] = Query(None),
    entity_id: Optional[UUID] = Query(None),
    bank_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """How the ledger was classified, and how much of it is actually settled."""
    from app.models.prediction import Prediction
    from app.categorization.decisions import REVIEW_THRESHOLD, needs_decision

    key = cache.user_key(current_user.id, "analytics-classification-health",
                         start_date, end_date, entity_id, bank_id)
    hit = cache.get(key)
    if hit is not None:
        return ClassificationHealthResponse(**hit)

    q = _scope_txn_query(db, current_user.id, entity_id, bank_id)
    if start_date:
        q = q.filter(Transaction.txn_date >= start_date)
    if end_date:
        q = q.filter(Transaction.txn_date <= end_date)

    rows = q.outerjoin(Prediction, Prediction.transaction_id == Transaction.id).with_entities(
        Transaction.category,
        Transaction.category_id,
        Transaction.category_confidence,
        Transaction.counterparty,
        Prediction.rule_used,
        Prediction.requires_review,
        Prediction.predicted_category,
    ).all()

    total = len(rows)
    if not total:
        out = ClassificationHealthResponse(
            total=0, categorised=0, settled=0, needs_person=0,
            review_threshold=REVIEW_THRESHOLD, evidence=[],
            counterparty_named=0, counterparty_unnamed=0,
        )
        cache.set(key, out.model_dump(mode="json"), _ANALYTICS_TTL_SECONDS)
        return out

    categorised = settled = named = 0
    conf_sum = 0.0
    conf_n = 0
    by_source: dict = {}

    for (category, category_id, confidence, counterparty,
         rule_used, requires_review, predicted) in rows:
        if category:
            categorised += 1
        if counterparty and str(counterparty).strip():
            named += 1
        if confidence is not None:
            conf_sum += float(confidence)
            conf_n += 1

        # The SAME predicate the Review Queue and Categories pages filter on,
        # imported rather than restated, so all three screens cannot drift.
        class _Row:
            pass
        probe = _Row()
        probe.category = category
        probe.category_id = category_id
        probe.category_confidence = confidence
        probe.legacy_category = None
        pred = None
        if rule_used is not None or requires_review is not None or predicted is not None:
            pred = _Row()
            pred.requires_review = bool(requires_review)
            pred.predicted_category = predicted
        if not needs_decision(probe, pred):
            settled += 1

        family = (str(rule_used).split(":", 1)[0] if rule_used else "")
        bucket = by_source.setdefault(family, {"count": 0, "conf_sum": 0.0, "conf_n": 0})
        bucket["count"] += 1
        if confidence is not None:
            bucket["conf_sum"] += float(confidence)
            bucket["conf_n"] += 1

    evidence = [
        EvidenceSource(
            source=family or "none",
            label=_EVIDENCE_LABELS.get(family, family or "No usable signal"),
            count=b["count"],
            share=round(b["count"] / total * 100, 2),
            avg_confidence=(round(b["conf_sum"] / b["conf_n"], 3) if b["conf_n"] else None),
            names_purpose=family not in _RAIL_ONLY,
        )
        for family, b in sorted(by_source.items(), key=lambda kv: kv[1]["count"], reverse=True)
    ]

    out = ClassificationHealthResponse(
        total=total,
        categorised=categorised,
        settled=settled,
        needs_person=total - settled,
        review_threshold=REVIEW_THRESHOLD,
        average_confidence=(round(conf_sum / conf_n, 3) if conf_n else None),
        evidence=evidence,
        counterparty_named=named,
        counterparty_unnamed=total - named,
    )
    cache.set(key, out.model_dump(mode="json"), _ANALYTICS_TTL_SECONDS)
    return out
