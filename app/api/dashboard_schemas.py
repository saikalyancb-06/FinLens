from typing import List, Optional, Dict, Any
from uuid import UUID
from datetime import date as date_type, datetime
from pydantic import BaseModel, Field, ConfigDict

class ProcessedTransactionResponse(BaseModel):
    id: UUID
    file_id: Optional[UUID] = None
    user_id: Optional[UUID] = None
    original_raw_text: Optional[str] = None
    date: Optional[datetime] = None
    description: str
    debit: float
    credit: float
    amount: float
    balance: float
    reference_number: Optional[str] = None
    transaction_type: Optional[str] = None
    rule_category: Optional[str] = None
    rule_confidence: Optional[float] = 0.0
    rule_matched: Optional[str] = None
    ml_category: Optional[str] = None
    ml_confidence: Optional[float] = 0.0
    ml_top_three: Optional[List[Any]] = None
    final_category: str
    confidence: float
    prediction_source: str
    reasoning: Optional[str] = None
    model_version: str
    processing_timestamp: datetime

    model_config = ConfigDict(from_attributes=True)

class DashboardSummaryResponse(BaseModel):
    total_transactions: int
    total_debit: float
    total_credit: float
    net_cash_flow: float
    total_files_processed: int
    uncategorized_count: int = 0
    uncategorized_txns: int = 0
    consolidated_liquidity: float = 0.0
    risk_alerts: int = 0
    anomalies: int = 0
    resolved_anomalies: int = 0
    pending_anomalies: int = 0
    # None means "nothing applicable to score yet" — an honest empty state,
    # rather than the 100% a default would imply.
    policy_compliance_pct: Optional[float] = None
    entities_count: int = 0
    accounts_count: int = 0

    # --- Finance-facing figures that replaced inflow/outflow/net on the tiles ---
    critical_anomalies: int = 0
    open_violations: int = 0
    critical_violations: int = 0
    unreconciled_count: int = 0
    unreconciled_value: float = 0.0
    reconciliation_coverage_pct: Optional[float] = None
    bank_charges: float = 0.0
    last_statement_date: Optional[str] = None
    data_age_days: Optional[int] = None
    avg_daily_burn: float = 0.0
    runway_days: Optional[int] = None
    net_movement: float = 0.0
    compliance_never_scanned: bool = False

class FilterMetadataResponse(BaseModel):
    categories: List[str]
    transaction_types: List[str]
    min_date: Optional[str] = None
    max_date: Optional[str] = None
    entities: List[Dict[str, Any]] = Field(default_factory=list)
    accounts: List[Dict[str, Any]] = Field(default_factory=list)

class MonthlySummaryItem(BaseModel):
    month: str
    total_debit: float
    total_credit: float
    net_cash_flow: float
    transaction_count: int

class CategoryBreakdownItem(BaseModel):
    category: str
    amount: float
    percentage: float
    count: int
    type: str  # expense or income

class CashFlowItem(BaseModel):
    period: str
    inflow: float
    outflow: float
    net: float


class AgeingBucket(BaseModel):
    """One ageing band of the bank reconciliation bridge."""
    label: str
    min_days: int
    max_days: Optional[int] = None      # None = open-ended (the 90+ bucket)
    bank_count: int
    bank_value: float
    book_count: int
    book_value: float
    total_count: int
    total_value: float
    exceptions: int


class UnreconciledAgeingResponse(BaseModel):
    # Ageing is measured from each run's period end, not from today. That is the
    # accounting convention - a reconciliation ages as at the date it was drawn
    # to - and it means this figure does not drift while nobody is looking.
    as_of: Optional[date_type] = None
    buckets: List[AgeingBucket]
    total_count: int
    total_value: float
    exceptions: int
    oldest_days: Optional[int] = None
    accounts_covered: int


class ForecastBacktestPoint(BaseModel):
    """One walk-forward comparison: what the forecast said, what actually happened.

    `as_of` is the cut-off the forecast was drawn at — the engine saw nothing
    after that date. `forecast_date` is `as_of` + the horizon, and `actual` is
    the cash balance that had genuinely materialised by then.
    """
    as_of: date_type
    forecast_date: date_type
    baseline: float                      # cash balance at the cut-off
    forecast_expected: float
    forecast_conservative: float
    actual: float
    error_pct: Optional[float] = None    # |forecast - actual| / |actual| * 100


class ForecastBacktestResponse(BaseModel):
    """Accuracy of the application's own forecast, measured against what happened.

    Nothing here is a stored forecast: the app has never persisted forecast
    snapshots, so an honest accuracy figure has to be *reconstructed*. It is
    reconstructed by replaying `compute_30_60_90_forecast` — the same engine the
    treasury report uses — at a series of past cut-off dates, with only the
    transactions that existed on each of those dates, and comparing each
    projection against the balance that actually arrived.

    `available` is False, with a `reason`, whenever there is not enough history
    to draw at least two such comparisons. It is never padded to look populated.
    """
    available: bool
    reason: Optional[str] = None
    horizon_days: int = 30
    method: Optional[str] = None
    # 100 - MAPE (mean absolute percentage error), clamped to 0..100. A standard
    # forecasting measure rather than a formula invented for this screen.
    accuracy_pct: Optional[float] = None
    accuracy_conservative_pct: Optional[float] = None
    points: List[ForecastBacktestPoint] = []
    points_count: int = 0


class CashPositionPoint(BaseModel):
    date: date_type
    balance: float


class CashPositionResponse(BaseModel):
    """Daily total cash held, plus the liquidity floor it has to stay above.

    `points` is the sum across accounts of each account's last posted running
    balance on or before that day — forward-filled, because an account that saw
    no movement has not lost its money, and summing only the accounts that moved
    would make total cash lurch every time one of them was quiet.

    `min_liquidity` is the sum of the minimum balances configured on the
    in-scope accounts. It is None when no account has one set: the dashboard
    draws no threshold line rather than inventing a floor.
    """
    points: List[CashPositionPoint] = []
    current_cash: Optional[float] = None
    as_of: Optional[date_type] = None
    min_liquidity: Optional[float] = None
    accounts_with_threshold: int = 0
    accounts_total: int = 0
    currency: Optional[str] = None
    currencies_mixed: bool = False


class OutflowBucket(BaseModel):
    label: str
    value: float
    count: int
    share: float                       # percent of economic outflows


class OutflowBreakdownResponse(BaseModel):
    """Economic cash outflows, grouped and ranked, computed server-side.

    This used to be derived in the browser from `/transactions?limit=10000`.
    That request carried 3.6 MB of transaction rows across the wire and cost
    around 21 seconds of CPU-bound JSON serialisation in a single-worker
    server — which starved every other request on the dashboard, including a
    256-byte account list that took 16 seconds to come back. All of it to draw
    ten bars. The aggregate is a couple of kilobytes.
    """
    available: bool
    rows: List[OutflowBucket] = []
    total: float = 0.0
    # Internal transfers are excluded from the economic view but reported, so
    # the panel can say what it left out rather than silently dropping money.
    excluded_value: float = 0.0
    excluded_count: int = 0
    categories_found: int = 0


class CounterpartyRow(BaseModel):
    name: str
    value: float
    count: int
    share: float


class CounterpartyOutflowsResponse(BaseModel):
    """Top counterparties by outflow. Same population as the breakdown above."""
    available: bool
    rows: List[CounterpartyRow] = []
    total: float = 0.0
    top5_share: Optional[float] = None
    top5_count: int = 0
    distinct: int = 0


class EvidenceSource(BaseModel):
    """One kind of evidence the classifier used, and how far it got on it."""
    source: str
    label: str
    count: int
    share: float
    avg_confidence: Optional[float] = None
    #: False when the answer names how the money MOVED rather than what it was
    #: for — a category of "NEFT Transfer" is a true statement about the rail
    #: and tells you nothing about the purpose.
    names_purpose: bool = True


class ClassificationHealthResponse(BaseModel):
    """Every row is categorised; not every row is settled. Both, side by side.

    `categorised` counts rows carrying a category at all. `settled` counts rows
    where nothing is still unsure — the same predicate the Review Queue and the
    Categories page filter on, imported from
    `app.categorization.decisions.needs_decision` rather than restated here so
    the three screens cannot drift apart.
    """
    total: int
    categorised: int
    settled: int
    needs_person: int
    review_threshold: float
    average_confidence: Optional[float] = None
    evidence: List[EvidenceSource] = []
    counterparty_named: int = 0
    counterparty_unnamed: int = 0
