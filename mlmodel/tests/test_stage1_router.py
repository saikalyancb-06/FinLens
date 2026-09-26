import pytest
from financial_parser.services.file_router import FileRouter, detect_file_format

def test_file_router_detection():
    router = FileRouter()

    # Test PDF, Excel, CSV detection
    assert detect_file_format(b"dummy pdf content", "statement.pdf") == "scanned_pdf"
    assert detect_file_format(b"dummy excel", "statement.xlsx") == "excel"
    assert detect_file_format(b"Date,Narration,Withdrawal", "statement.csv") == "csv"
    assert detect_file_format(b"dummy image", "scan.png") == "image"

    res = router.route(b"Date,Narration,Withdrawal", "statement.csv")
    assert res["detected_format"] == "csv"
    assert res["filename"] == "statement.csv"
