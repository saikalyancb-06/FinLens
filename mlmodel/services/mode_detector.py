def detect_mode(narration: str) -> str:
    """
    Mode Detection Rules:
    UPI -> UPI
    IMPS -> IMPS
    NEFT -> NEFT
    RTGS -> RTGS
    ATM -> ATM
    POS -> CARD
    CHEQUE / CHQ -> CHEQUE
    Otherwise -> OTHER
    """
    if not narration or not isinstance(narration, str):
        return "OTHER"
        
    narr_upper = narration.upper()
    if "UPI" in narr_upper:
        return "UPI"
    elif "IMPS" in narr_upper:
        return "IMPS"
    elif "NEFT" in narr_upper:
        return "NEFT"
    elif "RTGS" in narr_upper:
        return "RTGS"
    elif "ATM" in narr_upper:
        return "ATM"
    elif "POS" in narr_upper:
        return "CARD"
    elif "CHEQUE" in narr_upper or "CHQ" in narr_upper:
        return "CHEQUE"
    return "OTHER"
