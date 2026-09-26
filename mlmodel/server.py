import os
import json
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS

from layer1_extraction.extractor import extract_raw_document
from layer2_normalization.normalizer import NormalizationEngine
from layer3_classification.classifier import ClassificationEngine

app = Flask(__name__)
CORS(app)

normalizer = NormalizationEngine("configs/bob_current_account.yaml")
classifier = ClassificationEngine("configs/rules.yaml")

@app.route('/api/upload', methods=['POST'])
def handle_file_upload():
    """
    Decoupled 3-Layer Document Processing Pipeline API Endpoint.
    Layer 1 (Extraction) -> Layer 2 (Normalization + Pydantic Validation) -> Layer 3 (Classification)
    """
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    uploaded_file = request.files['file']
    filename = uploaded_file.filename
    if not filename:
        return jsonify({'error': 'Empty filename'}), 400

    try:
        file_bytes = uploaded_file.read()

        # Layer 1: Extraction Engine (format-agnostic)
        raw_res = extract_raw_document(file_bytes, filename)

        # Layer 2: Normalization Engine (Pydantic validated)
        valid_transactions = normalizer.normalize(raw_res)

        # Layer 3: Classification Engine (Rule Engine + ML)
        categorized_results = [classifier.classify_transaction(tx) for tx in valid_transactions]

        # Format JSON Response
        output_txs = []
        total_credits = 0.0
        total_debits = 0.0
        conf_scores = []
        cat_counts = {}
        monthly_spending = {}

        for idx, cat_tx in enumerate(categorized_results):
            tx = cat_tx.transaction
            w_val = float(tx.withdrawal) if tx.withdrawal is not None else 0.0
            d_val = float(tx.deposit) if tx.deposit is not None else 0.0
            b_val = float(tx.balance)

            total_credits += d_val
            total_debits += w_val
            conf_scores.append(cat_tx.confidence)

            cat_counts[cat_tx.category] = cat_counts.get(cat_tx.category, 0) + 1

            date_str = str(tx.transaction_date)
            month_key = date_str[:7] if len(date_str) >= 7 else "Unknown"
            monthly_spending[month_key] = monthly_spending.get(month_key, 0.0) + w_val

            output_txs.append({
                'id': idx + 1,
                'date': date_str,
                'narration': tx.narration,
                'withdrawal': w_val if w_val > 0 else None,
                'deposit': d_val if d_val > 0 else None,
                'balance': b_val,
                'amount': d_val if d_val > 0 else w_val,
                'category': cat_tx.category,
                # UI Layer displays percentage by multiplying confidence by 100
                'confidence': round(cat_tx.confidence * 100, 1),
                'is_low_confidence': cat_tx.confidence < 0.8,
                'engine_method': cat_tx.method
            })

        avg_conf_pct = round((sum(conf_scores) / len(conf_scores)) * 100, 1) if conf_scores else 0.0

        return jsonify({
            'transactions': output_txs,
            'statistics': {
                'total_transactions': len(output_txs),
                'total_credits': round(total_credits, 2),
                'total_debits': round(total_debits, 2),
                'avg_confidence': avg_conf_pct,
                'category_distribution': cat_counts,
                'monthly_spending': monthly_spending
            }
        })

    except Exception as e:
        logger.error(f"API Error processing {filename}: {e}", exc_info=True)
        return jsonify({'error': f"Processing error: {str(e)}", 'transactions': [], 'statistics': {}}), 500

@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'healthy', 'architecture': '3-Layer Decoupled Financial Pipeline'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
