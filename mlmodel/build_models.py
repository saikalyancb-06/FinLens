import os
import joblib
import pandas as pd
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import LabelEncoder
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestClassifier

from preprocessing import clean_text, extract_derived_features

def build_and_save_artifacts(file_path: str = 'data.csv', models_dir: str = 'models'):
    os.makedirs(models_dir, exist_ok=True)
    df = pd.read_csv(file_path)

    def map_target_category(row) -> str:
        nature = str(row.get('nature_of_transaction', '')).strip()
        head = str(row.get('head', '')).strip()
        narr = str(row.get('narration', '')).strip().upper()

        if nature == 'SALARY' or 'Salaries' in head: return 'Salary'
        elif 'GST' in head or 'TDS' in head: return 'Tax'
        elif 'Rent' in head: return 'Vendor Payment'
        elif nature == 'INTREST(OD)' or 'Interest' in head: return 'Interest'
        elif 'Bank Charge' in head or nature in ['Charges', 'BANK Charges']: return 'Bank Charges'
        elif nature == 'Self' or 'Contra' in head or 'Bank To Bank' in head: return 'Self Transfer'
        elif nature == 'Cash Deposit': return 'Cash Deposit'
        elif nature == 'BUSSINESS' or 'Sales Receipts' in head:
            if 'PHONEPE' in narr or 'PAYTM' in narr: return 'UPI Received'
            return 'Settlement'
        elif 'Food & Grocery' in head or nature in ['Grocery Purchase', 'Chicken Purchase', 'Fish Purchase', 'KAJU', 'PANNER ITEM', 'SOFTDRINK', 'VEGETABL']: return 'Food'
        elif 'Investment' in head or nature in ['INVESTMENT', 'CCTV']: return 'Investment'
        elif 'Electricity' in head or nature == 'Electricity Expense': return 'Utilities'
        elif nature in ['Gas Charges', 'GAS']: return 'Utilities'
        elif nature == 'Loan Given': return 'Loan Disbursement'
        elif nature == 'Loan Recovery': return 'Loan Recovery'
        elif nature == 'Income Tax Refund': return 'Refund'
        elif 'Repairs' in head or 'Equipment' in head: return 'Vendor Payment'
        elif nature == 'Partner Remuneration' or 'Partner Remuneration' in head: return 'Salary'
        return 'Others'

    df['target_category'] = df.apply(map_target_category, axis=1)

    clean_narrations = []
    amounts = []
    is_credits = []
    is_debits = []
    targets = []

    for _, row in df.iterrows():
        w = row.get('withdrawal', 0)
        d = row.get('deposit', 0)
        feats = extract_derived_features(row.get('narration', ''), w, d)
        
        clean_narrations.append(feats['cleaned_narration'])
        amounts.append(feats['amount'])
        is_credits.append(feats['is_credit'])
        is_debits.append(feats['is_debit'])
        targets.append(row['target_category'])

    tfidf = TfidfVectorizer(max_features=5000, ngram_range=(1, 2))
    X_tfidf = tfidf.fit_transform(clean_narrations).toarray()

    X_num = np.column_stack([amounts, is_credits, is_debits])
    X = np.hstack([X_tfidf, X_num])

    label_encoder = LabelEncoder()
    y = label_encoder.fit_transform(targets)

    rf_model = RandomForestClassifier(n_estimators=100, random_state=42)
    rf_model.fit(X, y)

    joblib.dump(tfidf, os.path.join(models_dir, 'tfidf.pkl'))
    joblib.dump(label_encoder, os.path.join(models_dir, 'label_encoder.pkl'))
    joblib.dump(rf_model, os.path.join(models_dir, 'model.pkl'))

    print(f"Artifacts successfully saved to '{models_dir}/'")

if __name__ == '__main__':
    build_and_save_artifacts()
