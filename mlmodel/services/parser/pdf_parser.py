import io
import re
import logging
import pandas as pd

logger = logging.getLogger(__name__)

def parse_pdf_stage1_pymupdf(file_content: bytes) -> pd.DataFrame:
    """
    Stage 1: PyMuPDF word-coordinate extraction page.get_text("words").
    Each word: (x0, y0, x1, y1, text, block_no, line_no, word_no).
    """
    try:
        import fitz
        doc = fitz.open(stream=file_content, filetype="pdf")
        all_words = []

        for page_idx, page in enumerate(doc):
            # word tuple: (x0, y0, x1, y1, "word", block_no, line_no, word_no)
            words = page.get_text("words")
            for w in words:
                all_words.append({
                    'page': page_idx,
                    'x0': w[0],
                    'y0': w[1],
                    'x1': w[2],
                    'y1': w[3],
                    'text': w[4]
                })

        if not all_words:
            return pd.DataFrame()

        return _reconstruct_table_from_words(all_words)
    except Exception as e:
        logger.warning(f"Stage 1 (PyMuPDF) failed: {e}")
        return pd.DataFrame()

def parse_pdf_stage2_pdfplumber(file_content: bytes) -> pd.DataFrame:
    """
    Stage 2: pdfplumber extract_words() fallback.
    """
    try:
        import pdfplumber
        all_words = []
        with pdfplumber.open(io.BytesIO(file_content)) as pdf:
            for page_idx, page in enumerate(pdf.pages):
                words = page.extract_words(x_tolerance=3, y_tolerance=3)
                for w in words:
                    all_words.append({
                        'page': page_idx,
                        'x0': w['x0'],
                        'y0': w['top'],
                        'x1': w['x1'],
                        'y1': w['bottom'],
                        'text': w['text']
                    })

        if not all_words:
            return pd.DataFrame()

        return _reconstruct_table_from_words(all_words)
    except Exception as e:
        logger.warning(f"Stage 2 (pdfplumber) failed: {e}")
        return pd.DataFrame()

def parse_pdf_stage3_camelot(file_content: bytes) -> pd.DataFrame:
    """
    Stage 3: Camelot table extraction fallback.
    """
    try:
        import camelot
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            tmp.write(file_content)
            tmp_path = tmp.name

        tables = camelot.read_pdf(tmp_path, pages='all', flavor='lattice')
        if not tables or len(tables) == 0:
            tables = camelot.read_pdf(tmp_path, pages='all', flavor='stream')

        os.remove(tmp_path)

        dfs = []
        for t in tables:
            if not t.df.empty:
                dfs.append(t.df)

        if dfs:
            combined = pd.concat(dfs, ignore_index=True)
            return combined
        return pd.DataFrame()
    except Exception as e:
        logger.warning(f"Stage 3 (Camelot) failed: {e}")
        return pd.DataFrame()

def parse_pdf_stage4_ocr(file_content: bytes) -> pd.DataFrame:
    """
    Stage 4: Tesseract OCR fallback for scanned images inside PDF.
    """
    try:
        import fitz
        import pytesseract
        from PIL import Image

        doc = fitz.open(stream=file_content, filetype="pdf")
        ocr_text = ""

        for page in doc:
            pix = page.get_pixmap()
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            ocr_text += pytesseract.image_to_string(img) + "\n"

        return _parse_raw_text_lines(ocr_text)
    except Exception as e:
        logger.warning(f"Stage 4 (OCR) failed: {e}")
        return pd.DataFrame()

def _reconstruct_table_from_words(words_list: list) -> pd.DataFrame:
    """
    Groups words by Y coordinate (y-level) and page, sorts by X coordinate,
    and dynamically routes words into Date, Narration, Withdrawal, Deposit, Balance.
    Multi-line narrations are merged until the next date regex transaction start line.
    """
    lines_by_y = {}
    for w in words_list:
        key = (w['page'], round(w['y0'], 1))
        if key not in lines_by_y:
            lines_by_y[key] = []
        lines_by_y[key].append(w)

    sorted_keys = sorted(lines_by_y.keys(), key=lambda k: (k[0], k[1]))

    header_x = {
        'date': (0, 100),
        'narration': (100, 320),
        'withdrawal': (320, 420),
        'deposit': (420, 500),
        'balance': (500, 570),
        'cr_dr': (570, 600)
    }

    # Header bounding box detection
    for key in sorted_keys:
        line_str = " ".join([w['text'] for w in sorted(lines_by_y[key], key=lambda x: x['x0'])])
        upper = line_str.upper()
        if 'DATE' in upper and ('NARRATION' in upper or 'PARTICULAR' in upper or 'BALANCE' in upper):
            for w in sorted(lines_by_y[key], key=lambda x: x['x0']):
                t = w['text'].upper()
                if 'DATE' in t: header_x['date'] = (0, w['x1'] + 20)
                elif 'NARRATION' in t or 'PARTICULAR' in t: header_x['narration'] = (w['x0'] - 10, w['x1'] + 100)
                elif 'WITHDRAWAL' in t or 'DEBIT' in t: header_x['withdrawal'] = (w['x0'] - 30, w['x1'] + 20)
                elif 'DEPOSIT' in t or 'CREDIT' in t: header_x['deposit'] = (w['x0'] - 30, w['x1'] + 20)
                elif 'BALANCE' in t: header_x['balance'] = (w['x0'] - 20, w['x1'] + 20)

    date_regex = re.compile(r'^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$')
    transactions = []
    curr = None

    for key in sorted_keys:
        words_in_line = sorted(lines_by_y[key], key=lambda x: x['x0'])
        line_text = " ".join([w['text'] for w in words_in_line]).strip()

        if not line_text: continue
        upper = line_text.upper()
        if ('DATE' in upper and 'NARRATION' in upper) or 'PAGE ' in upper or 'STATEMENT OF ACCOUNT' in upper:
            continue

        first_w = words_in_line[0]['text'].strip()

        if date_regex.match(first_w):
            if curr: transactions.append(curr)

            narr_p, w_p, d_p, b_p, cr_p = [], [], [], [], []
            for w in words_in_line:
                t = w['text'].strip()
                x_center = (w['x0'] + w['x1']) / 2.0

                if x_center < header_x['narration'][0]: pass
                elif header_x['narration'][0] <= x_center < header_x['withdrawal'][0]: narr_p.append(t)
                elif header_x['withdrawal'][0] <= x_center < header_x['deposit'][0]: w_p.append(t)
                elif header_x['deposit'][0] <= x_center < header_x['balance'][0]: d_p.append(t)
                elif header_x['balance'][0] <= x_center < header_x['cr_dr'][0]: b_p.append(t)
                else: cr_p.append(t)

            curr = {
                'date': first_w,
                'narration': " ".join(narr_p),
                'withdrawal': "".join(w_p),
                'deposit': "".join(d_p),
                'balance': "".join(b_p),
                'balance_dr_cr': "".join(cr_p)
            }
        else:
            if curr:
                add_narr = []
                for w in words_in_line:
                    t = w['text'].strip()
                    x_center = (w['x0'] + w['x1']) / 2.0
                    if x_center < header_x['withdrawal'][0]:
                        add_narr.append(t)
                    elif t in ['Cr', 'Dr', 'CR', 'DR'] and not curr['balance_dr_cr']:
                        curr['balance_dr_cr'] = t
                if add_narr:
                    curr['narration'] += " " + " ".join(add_narr)

    if curr: transactions.append(curr)

    return pd.DataFrame(transactions) if transactions else pd.DataFrame()

def _parse_raw_text_lines(text: str) -> pd.DataFrame:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    date_regex = re.compile(r'^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$')
    records = []
    curr = None

    for line in lines:
        parts = line.split()
        if not parts: continue
        if date_regex.match(parts[0]):
            if curr: records.append(curr)
            curr = {'date': parts[0], 'narration': " ".join(parts[1:]), 'withdrawal': 0.0, 'deposit': 0.0, 'balance': 0.0}
        elif curr:
            curr['narration'] += " " + line

    if curr: records.append(curr)
    return pd.DataFrame(records) if records else pd.DataFrame()

def parse_pdf_file(file_content: bytes, filename: str = "") -> pd.DataFrame:
    """
    Multi-Stage PDF Extraction Pipeline:
    Stage 1: PyMuPDF word coordinates
    Stage 2: pdfplumber word coordinates
    Stage 3: Camelot table parser
    Stage 4: Tesseract OCR fallback
    """
    logger.info(f"Starting Multi-Stage PDF Parsing for {filename}")

    # Stage 1: PyMuPDF
    df = parse_pdf_stage1_pymupdf(file_content)
    if not df.empty:
        logger.info("PDF extraction succeeded at Stage 1 (PyMuPDF)")
        return df

    # Stage 2: pdfplumber
    df = parse_pdf_stage2_pdfplumber(file_content)
    if not df.empty:
        logger.info("PDF extraction succeeded at Stage 2 (pdfplumber)")
        return df

    # Stage 3: Camelot
    df = parse_pdf_stage3_camelot(file_content)
    if not df.empty:
        logger.info("PDF extraction succeeded at Stage 3 (Camelot)")
        return df

    # Stage 4: OCR Fallback
    df = parse_pdf_stage4_ocr(file_content)
    if not df.empty:
        logger.info("PDF extraction succeeded at Stage 4 (OCR)")
        return df

    logger.warning("All PDF extraction stages produced empty output.")
    return pd.DataFrame(columns=['date', 'narration', 'withdrawal', 'deposit', 'balance'])
