"""Stateless financial analysis over a list of `CanonicalTxn`.

One entry point:

    from app.b2b.analysis import analyze
    result = analyze(txns, statement_meta=..., parse_quality=..., loan=...)

`result.data` is the response payload, `result.quality` carries
`overall_confidence` and `reconciliation_status`, and `result.raw` keeps the
paise-precise internals every figure was derived from.

No module in this package opens a database session, and the two that reach code
which imports the ORM (`balances`, `risk`) do so lazily and degrade to
`unavailable` if that import fails, so `import app.b2b.analysis` works with no
database configured.
"""
from app.b2b.analysis.engine import (  # noqa: F401
    FAILED,
    NOT_VERIFIABLE,
    PASSED,
    AnalysisResult,
    analyze,
)
from app.b2b.analysis.enrich import enrich  # noqa: F401

__all__ = ["analyze", "enrich", "AnalysisResult",
           "PASSED", "FAILED", "NOT_VERIFIABLE"]
