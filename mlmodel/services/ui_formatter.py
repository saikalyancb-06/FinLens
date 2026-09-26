import pandas as pd
import numpy as np
from services.mode_detector import detect_mode
from services.merchant_extractor import extract_merchant
from services.classifier import TransactionClassifier

def process_and_format_transactions(df: pd.DataFrame) -> dict:
    """
    Runs full downstream pipeline over normalized DataFrame:
    Cleaner -> Feature Engineering -> Mode Detection -> Merchant Extraction -> Rule Engine -> ML Model
    Generates statistics & analytics payloads for UI.
    """
    classifier = TransactionClassifier()
    records = []
    
    cat_counts = {}
    monthly_spending = {}
    merchant_counts = {}
    
    total_credits = 0.0
    total_debits = 0.0
    conf_scores = []

    for idx, row in df.iterrows():
        date_str = str(row.get('date', ''))
        narr_str = str(row.get('narration', ''))
        
        # Safely extract floats (convert NaN to 0.0)
        w_raw = row.get('withdrawal', 0.0)
        d_raw = row.get('deposit', 0.0)
        bal_raw = row.get('balance', 0.0)

        w_val = 0.0 if pd.isnull(w_raw) or w_raw is None else float(w_raw)
        d_val = 0.0 if pd.isnull(d_raw) or d_raw is None else float(d_raw)
        bal_val = 0.0 if pd.isnull(bal_raw) or bal_raw is None else float(bal_raw)

        # Mode & Merchant Detection
        mode = detect_mode(narr_str)
        merchant = extract_merchant(narr_str)

        # Rule Engine & ML Classification
        clf_res = classifier.classify(narr_str, w_val, d_val)
        category = clf_res['category']
        confidence = clf_res['confidence']
        is_low_conf = clf_res['is_low_confidence']
        engine_method = clf_res['method']
        conf_scores.append(confidence)

        # Financial totals
        total_credits += d_val
        total_debits += w_val

        # Category distribution
        cat_counts[category] = cat_counts.get(category, 0) + 1

        # Merchant leaderboard
        if merchant != "UNKNOWN" and merchant not in ["CR", "DR"]:
            merchant_counts[merchant] = merchant_counts.get(merchant, 0.0) + max(w_val, d_val)

        # Monthly spending
        month_key = date_str
        if len(date_str) == 10 and date_str[2] == '/' and date_str[5] == '/':
            month_key = date_str[6:] + '-' + date_str[3:5]
        elif len(date_str) >= 7:
            month_key = date_str[:7]
        monthly_spending[month_key] = monthly_spending.get(month_key, 0.0) + w_val

        records.append({
            'id': idx + 1,
            'date': date_str,
            'narration': narr_str,
            'withdrawal': w_val if w_val > 0 else None,
            'deposit': d_val if d_val > 0 else None,
            'balance': bal_val,
            'amount': d_val if d_val > 0 else w_val,
            'mode': mode,
            'merchant': merchant,
            'category': category,
            'confidence': confidence,
            'is_low_confidence': is_low_conf,
            'engine_method': engine_method
        })

    avg_conf = round(sum(conf_scores) / len(conf_scores), 1) if conf_scores else 0.0

    # Sort top merchants
    sorted_merchants = dict(sorted(merchant_counts.items(), key=lambda item: item[1], reverse=True)[:5])

    return {
        'transactions': records,
        'statistics': {
            'total_transactions': len(records),
            'total_credits': round(total_credits, 2),
            'total_debits': round(total_debits, 2),
            'avg_confidence': avg_conf,
            'category_distribution': cat_counts,
            'monthly_spending': monthly_spending,
            'top_merchants': sorted_merchants
        }
    }
