"""Which parser handles which format, and what `GET /v1/formats` promises.

`SUPPORTED_FORMATS` is a published document, not an internal table. An
integrator reads it to decide what to send, so every entry states its real
limitations — including the ones that are awkward: OFX and CAMT cannot have
their balances verified, scanned PDFs need OCR binaries this deployment may not
have, and `.xls` needs an optional engine. A list that promised clean support
for all of those would generate support tickets instead of preventing them.

Formats that are deliberately *not* accepted are published too, in
`UNSUPPORTED_FORMATS`, with the reason. "Send us your ZIP of statements" is a
reasonable thing for a client to assume; being told plainly that archives are
rejected is cheaper for both sides than a 422 they have to interpret.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from app.b2b.errors import ApiError, UNSUPPORTED_FILE_FORMAT
from app.b2b.parsers import camt, delimited, jsonfmt, legacy, ofx
from app.b2b.parsers.base import ParseOutput


def _csv_dispatch(path: str, *, password: Optional[str] = None,
                  currency: str = "INR") -> ParseOutput:
    """Route a CSV by its actual delimiter.

    A comma CSV goes through the production pipeline, which has years of
    real-statement handling behind it. A semicolon, tab or pipe CSV cannot: the
    pipeline reads it with `csv.reader(..., delimiter=',')`, gets one wide
    column per line, maps no headers and returns zero transactions with no
    error explaining why. Those go to the delimited parser, which sniffs the
    separator first and then runs the identical header/amount/validation path.
    """
    from app.b2b.parsers.delimited import sniff_delimiter

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            sample = handle.read(65_536)
    except OSError:
        sample = ""

    delimiter, _confidence, _reason = sniff_delimiter(sample)
    if delimiter in (";", "\t", "|"):
        return delimited.parse(path, password=password, currency=currency)
    return legacy.parse(path, password=password, currency=currency)


#: format id -> callable(path, *, password, currency) -> ParseOutput
PARSERS: Dict[str, Callable[..., ParseOutput]] = {
    "pdf": legacy.parse,
    "xlsx": legacy.parse,
    "xls": legacy.parse,
    "csv": _csv_dispatch,
    "tsv": delimited.parse,
    "txt": delimited.parse,
    "json": jsonfmt.parse,
    "ofx": ofx.parse,
    "qfx": ofx.parse,
    "xml_camt": camt.parse,
}


SUPPORTED_FORMATS: List[Dict[str, Any]] = [
    {
        "id": "pdf",
        "extensions": [".pdf"],
        "mime_types": ["application/pdf"],
        "description": "Bank statement PDF. Table extraction first, text-layout "
                       "reconstruction as a fallback.",
        "notes": [
            "Password-protected PDFs are supported; send the password with the request.",
            "Scanned (image-only) PDFs require OCR, which depends on the tesseract "
            "and poppler system binaries being installed on the deployment. Where "
            "they are absent, an image-only PDF yields no transactions rather than "
            "a partial result.",
            "OCR-recovered rows are returned with a lower parse confidence and an "
            "OCR_USED warning; narrations and amounts may contain character errors.",
            "Balance continuity is checked only where the layout has a running "
            "balance column.",
        ],
    },
    {
        "id": "xlsx",
        "extensions": [".xlsx", ".xlsm"],
        "mime_types": [
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ],
        "description": "Excel workbook (OOXML). Multi-sheet files are scanned and "
                       "the sheet with the most transaction rows is used.",
        "notes": [
            "Only one sheet is parsed per file; a workbook holding two accounts "
            "returns the larger one.",
            "Formulas are read as their cached values; a workbook saved without "
            "cached values will show blank amounts.",
        ],
    },
    {
        "id": "xls",
        "extensions": [".xls"],
        "mime_types": ["application/vnd.ms-excel"],
        "description": "Legacy Excel 97-2003 workbook (OLE2 compound document).",
        "notes": [
            "Requires the optional xlrd>=2.0.1 engine. Where it is not installed "
            "the request is rejected with UNSUPPORTED_FILE_FORMAT rather than "
            "returning an empty result.",
            "Many banks export an HTML table with an .xls extension. Those are "
            "detected by content and read as HTML tables.",
        ],
    },
    {
        "id": "csv",
        "extensions": [".csv"],
        "mime_types": ["text/csv", "application/csv"],
        "description": "Comma-separated statement export. Around 130 column-header "
                       "aliases are recognised, including Indian bank variants.",
        "notes": [
            "The delimiter is sniffed from content: semicolon, tab and pipe files "
            "sent with a .csv extension are handled.",
            "Indian lakh-crore digit grouping (1,23,456.78), CR/DR suffixes and "
            "parenthesised negatives are parsed.",
            "Rows whose narration wraps onto a following line are merged upward.",
        ],
    },
    {
        "id": "tsv",
        "extensions": [".tsv", ".tab"],
        "mime_types": ["text/tab-separated-values"],
        "description": "Tab-separated statement export.",
        "notes": [
            "Same header aliases, amount parsing and validation as the CSV path.",
        ],
    },
    {
        "id": "txt",
        "extensions": [".txt", ".dat"],
        "mime_types": ["text/plain"],
        "description": "Delimited plain-text export where the separator is "
                       "detected from the content.",
        "notes": [
            "Only delimited text is supported. Fixed-width column layouts and "
            "free-form printed statements are not parsed and return PARSE_FAILED.",
        ],
    },
    {
        "id": "json",
        "extensions": [".json"],
        "mime_types": ["application/json"],
        "description": "JSON statement as a top-level array, a {\"transactions\": []} "
                       "object, or {\"data\": {\"transactions\": []}}.",
        "notes": [
            "Field names are matched against the same alias table as spreadsheet "
            "columns, so date/description/amount/type/balance/currency work, as do "
            "narration, withdrawal, deposit and chq/ref no.",
            "Nested objects are flattened one level per dot; arrays inside a "
            "transaction object are ignored.",
            "If any amount in the document is negative, the file is read as using "
            "signed amounts and unlabelled rows take their direction from the sign.",
            "A per-row currency field is honoured; mixed currencies raise a "
            "MULTI_CURRENCY warning and are not summed together.",
        ],
    },
    {
        "id": "ofx",
        "extensions": [".ofx"],
        "mime_types": ["application/x-ofx"],
        "description": "Open Financial Exchange, both 1.x (SGML) and 2.x (XML).",
        "notes": [
            "Direction is taken from the sign of TRNAMT, which is reliable, rather "
            "than from TRNTYPE, which issuers use inconsistently.",
            "OFX carries no per-transaction running balance. Balance continuity is "
            "therefore reported as unverifiable (null), never as passed, and every "
            "response carries a CONTINUITY_UNVERIFIABLE warning.",
            "LEDGERBAL supplies the statement closing balance; DTSTART/DTEND the "
            "declared period; CURDEF the currency.",
            "Investment statements (INVSTMTRS) are not parsed; only bank and "
            "credit-card statement responses are.",
        ],
    },
    {
        "id": "qfx",
        "extensions": [".qfx"],
        "mime_types": ["application/vnd.intu.qfx"],
        "description": "Quicken's OFX variant. Parsed by the OFX reader; the "
                       "transaction payload is identical.",
        "notes": [
            "Quicken-specific extension blocks (INTU.*) are ignored.",
            "Same limitation as OFX: no running balance, so continuity is "
            "unverifiable.",
        ],
    },
    {
        "id": "xml_camt",
        "extensions": [".xml"],
        "mime_types": ["application/xml", "text/xml"],
        "description": "ISO 20022 CAMT.053 bank-to-customer statement. Any "
                       "camt.053.001.xx version is accepted.",
        "notes": [
            "Elements are matched by local name, so the schema version in the "
            "namespace does not need to be known in advance.",
            "One transaction is emitted per Ntry, which is what the bank booked. "
            "An Ntry whose NtryDtls holds several individually-priced TxDtls is "
            "expanded into one transaction per TxDtls.",
            "Opening and closing balances come from Bal entries coded OPBD/PRCD "
            "and CLBD; there is no running balance per entry, so continuity is "
            "reported as unverifiable.",
            "Multi-currency statements are supported per row and raise a "
            "MULTI_CURRENCY warning.",
            "DTDs and XML entities are rejected outright (XXE protection); resend "
            "without a DOCTYPE.",
            "Other ISO 20022 messages — camt.052 intraday, camt.054 notifications, "
            "pain.* payment instructions — are not parsed.",
        ],
    },
]


UNSUPPORTED_FORMATS: List[Dict[str, Any]] = [
    {
        "id": "zip",
        "extensions": [".zip"],
        "reason": "Archives are not accepted. Upload one statement per request so "
                  "that per-file errors, warnings and billing are unambiguous.",
    },
    {
        "id": "xml_other",
        "extensions": [".xml"],
        "reason": "Only CAMT.053 XML is parsed. Generic XML, HTML tables saved as "
                  ".xml, and other ISO 20022 messages are rejected.",
    },
    {
        "id": "image",
        "extensions": [".png", ".jpg", ".jpeg", ".tiff"],
        "reason": "Photographs and screenshots of statements are not accepted; "
                  "send the source PDF or export.",
    },
    {
        "id": "docx",
        "extensions": [".doc", ".docx"],
        "reason": "Word documents are not a statement interchange format.",
    },
]

#: Every extension any supported parser claims, for a cheap membership test.
SUPPORTED_EXTENSIONS = {
    ext for entry in SUPPORTED_FORMATS for ext in entry["extensions"]
}


def get_parser(format_id: str) -> Callable[..., ParseOutput]:
    """The parse callable for a detected format id.

    Raises `UNSUPPORTED_FILE_FORMAT` for anything else, including the formats
    listed in `UNSUPPORTED_FORMATS`, whose published reason is repeated in the
    error message so the client does not have to look it up.
    """
    parser = PARSERS.get((format_id or "").lower())
    if parser is not None:
        return parser

    for entry in UNSUPPORTED_FORMATS:
        if entry["id"] == format_id:
            raise ApiError(UNSUPPORTED_FILE_FORMAT, entry["reason"])

    supported = ", ".join(sorted(PARSERS))
    raise ApiError(
        UNSUPPORTED_FILE_FORMAT,
        f"'{format_id}' is not a supported statement format; supported formats "
        f"are: {supported}",
    )


def format_entry(format_id: str) -> Optional[Dict[str, Any]]:
    """The published description of one format, or None."""
    for entry in SUPPORTED_FORMATS:
        if entry["id"] == format_id:
            return entry
    return None
