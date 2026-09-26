"""Format parsers for the B2B API.

Every parser in this package has the same signature —
`parse(path, *, password=None, currency="INR") -> ParseOutput` — and the
registry is the only thing that decides which one runs. Import the registry,
not a parser module, from outside this package.
"""
from app.b2b.parsers.base import ParseOutput

__all__ = ["ParseOutput", "get_parser", "SUPPORTED_FORMATS", "UNSUPPORTED_FORMATS"]


def __getattr__(name):
    # The registry imports every parser module, and `legacy` pulls in the ML
    # decision engine. Resolving these lazily keeps `import app.b2b.parsers`
    # (which `detect` does, for the delimiter sniffer) from paying that cost.
    if name in ("get_parser", "SUPPORTED_FORMATS", "UNSUPPORTED_FORMATS"):
        from app.b2b.parsers import registry
        return getattr(registry, name)
    raise AttributeError(name)
