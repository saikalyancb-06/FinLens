import re

def extract_merchant(narration: str) -> str:
    """
    Extracts merchant name using regex pattern heuristics.
    Examples:
    UPI/.../SWIGGY -> SWIGGY
    UPI/.../ZOMATO -> ZOMATO
    NEFT...RESILIENT INNOVATIONS -> RESILIENT INNOVATIONS
    EBANK:SELF -> SELF
    """
    if not narration or not isinstance(narration, str):
        return "UNKNOWN"
        
    narr_upper = narration.upper()

    # Rule/Regex patterns
    if "SWIGGY" in narr_upper: return "SWIGGY"
    if "ZOMATO" in narr_upper: return "ZOMATO"
    if "RESILIENT INNOVATIONS" in narr_upper: return "RESILIENT INNOVATIONS"
    if "EBANK:SELF" in narr_upper or "SELF" in narr_upper: return "SELF"
    if "BHARATPE" in narr_upper: return "BHARATPE"
    if "AMAZON" in narr_upper: return "AMAZON"
    if "FLIPKART" in narr_upper: return "FLIPKART"
    if "INDIAN OIL" in narr_upper: return "INDIAN OIL"
    if "HPCL" in narr_upper: return "HPCL"
    if "BESCOM" in narr_upper: return "BESCOM"
    if "BWSSB" in narr_upper: return "BWSSB"
    if "CONCEPT STUDIO" in narr_upper: return "CONCEPT STUDIO"

    # Regex heuristic for UPI / NEFT formats (e.g. UPI/12345/MERCHANT_NAME)
    match_upi = re.search(r'UPI/[^/]+(?:/[^/]+)*/([A-Z0-9\s]+)', narr_upper)
    if match_upi:
        return match_upi.group(1).strip()

    match_neft = re.search(r'NEFT-[A-Z0-9]+-([A-Z0-9\s]+)', narr_upper)
    if match_neft:
        return match_neft.group(1).strip()

    # Fallback to first major word block
    words = [w for w in re.sub(r'[^A-Z\s]', '', narr_upper).split() if len(w) > 2]
    return words[0] if words else "UNKNOWN"
