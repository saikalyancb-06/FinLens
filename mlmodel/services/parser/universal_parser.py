import io
import logging
import pandas as pd
from services.parser.pdf_parser import parse_pdf_file
from services.parser.excel_parser import parse_excel_file
from services.parser.csv_parser import parse_csv_file
from services.parser.word_parser import parse_word_file
from services.parser.image_parser import parse_image_file
from services.transaction_builder import validate_and_build_transaction_df

logger = logging.getLogger(__name__)

def parse_universal_file(file_content: bytes, filename: str) -> pd.DataFrame:
    """
    Unified File Dispatcher & Parser Entrypoint.
    Detects file extension -> Routes to specialized parser -> Validates schema via transaction_builder.
    
    Supported Extensions:
    - PDF (.pdf)
    - Excel (.xlsx, .xls)
    - CSV (.csv)
    - Word (.docx, .doc)
    - Image (.png, .jpg, .jpeg)

    Always returns a clean pandas.DataFrame:
    [date, narration, withdrawal, deposit, balance]
    """
    ext = filename.lower().split('.')[-1] if '.' in filename else ''
    logger.info(f"Dispatching file '{filename}' with extension '.{ext}'")

    raw_df = pd.DataFrame()

    try:
        if ext == 'pdf':
            raw_df = parse_pdf_file(file_content, filename)
        elif ext in ['xlsx', 'xls']:
            raw_df = parse_excel_file(file_content, filename)
        elif ext == 'csv':
            raw_df = parse_csv_file(file_content, filename)
        elif ext in ['docx', 'doc']:
            raw_df = parse_word_file(file_content, filename)
        elif ext in ['png', 'jpg', 'jpeg']:
            raw_df = parse_image_file(file_content, filename)
        elif ext == 'txt':
            raw_df = parse_csv_file(file_content, filename)
        else:
            logger.error(f"Unsupported file type: {filename}")
            return pd.DataFrame(columns=['date', 'narration', 'withdrawal', 'deposit', 'balance'])

        # Enforce validation and standardized DataFrame output schema
        clean_df = validate_and_build_transaction_df(raw_df)
        return clean_df

    except Exception as e:
        logger.error(f"Universal file dispatcher exception for {filename}: {e}", exc_info=True)
        return pd.DataFrame(columns=['date', 'narration', 'withdrawal', 'deposit', 'balance'])
