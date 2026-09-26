import os
import joblib
import pandas as pd
import numpy as np

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import LabelEncoder
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, precision_recall_fscore_support, classification_report, confusion_matrix

from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.svm import SVC
from xgboost import XGBClassifier
from lightgbm import LGBMClassifier
from catboost import CatBoostClassifier

from preprocessing import clean_text, extract_derived_features
from rules import RuleEngine

def load_data(file_path: str) -> pd.DataFrame:
    """Loads CSV dataset."""
    df = pd.read_csv(file_path)
    return df

def map_target_category(row) -> str:
    """
    Maps dataset targets ('nature_of_transaction' / 'head') to prompt target categories.
    """
    nature = str(row.get('nature_of_transaction', '')).strip()
    head = str(row.get('head', '')).strip()
    narr = str(row.get('narration', '')).strip().upper()

    # Domain mapping logic
    if nature == 'SALARY' or 'Salaries' in head:
        return 'Salary'
    elif 'GST' in head or 'TDS' in head:
        return 'Tax'
    elif 'Rent' in head:
        return 'Vendor Payment'
    elif nature == 'INTREST(OD)' or 'Interest' in head:
        return 'Interest'
    elif 'Bank Charge' in head or nature == 'Charges' or nature == 'BANK Charges':
        return 'Bank Charges'
    elif nature == 'Self' or 'Contra' in head or 'Bank To Bank' in head:
        return 'Self Transfer'
    elif nature == 'Cash Deposit':
        return 'Cash Deposit'
    elif nature == 'BUSSINESS' or 'Sales Receipts' in head:
        if 'PHONEPE' in narr or 'PAYTM' in narr:
            return 'UPI Received'
        return 'Settlement'
    elif 'Food & Grocery' in head or nature in ['Grocery Purchase', 'Chicken Purchase', 'Fish Purchase', 'KAJU', 'PANNER ITEM', 'SOFTDRINK', 'VEGETABL']:
        return 'Food'
    elif 'Investment' in head or nature == 'INVESTMENT' or nature == 'CCTV':
        return 'Investment'
    elif 'Electricity' in head or nature == 'Electricity Expense':
        return 'Utilities'
    elif nature == 'Gas Charges' or nature == 'GAS':
        return 'Utilities'
    elif nature == 'Loan Given':
        return 'Loan Disbursement'
    elif nature == 'Loan Recovery':
        return 'Loan Recovery'
    elif nature == 'Income Tax Refund':
        return 'Refund'
    elif 'Repairs' in head or 'Equipment' in head:
        return 'Vendor Payment'
    elif nature == 'Partner Remuneration' or 'Partner Remuneration' in head:
        return 'Salary'
    
    return 'Others'

def train_and_evaluate(df: pd.DataFrame, models_dir: str = 'models'):
    os.makedirs(models_dir, exist_ok=True)
    rule_engine = RuleEngine()

    print("--- STEP 1 & 2: Rule Evaluation & Preprocessing ---")
    if 'target_category' not in df.columns:
        df['target_category'] = df.apply(map_target_category, axis=1)

    # Filter out rows matched by Rule Engine to prioritize ML training on non-rule matched data
    unmatched_rows = []
    rule_matched_count = 0

    for idx, row in df.iterrows():
        narr = str(row.get('narration', ''))
        rule_match = rule_engine.match(narr)
        if rule_match:
            rule_matched_count += 1
        # Include all rows for comprehensive ML training
        unmatched_rows.append(idx)

    print(f"Total Transactions: {len(df)}")
    print(f"Transactions matching pre-ML Rules: {rule_matched_count}")

    # Prepare features
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

    # TF-IDF Feature Extraction (max_features=5000, ngram_range=(1,2))
    tfidf = TfidfVectorizer(max_features=5000, ngram_range=(1, 2))
    X_tfidf = tfidf.fit_transform(clean_narrations).toarray()

    # Combine TF-IDF with Numerical Features
    X_num = np.column_stack([amounts, is_credits, is_debits])
    X = np.hstack([X_tfidf, X_num])

    # Encode target labels
    label_encoder = LabelEncoder()
    y = label_encoder.fit_transform(targets)

    # 80/20 Train-Test Split (safe stratification)
    class_counts = pd.Series(y).value_counts()
    can_stratify = (class_counts.min() >= 2)
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.20, random_state=42, stratify=y if can_stratify else None
    )

    print("\n--- STEP 5: Model Comparisons ---")
    candidate_models = {
        'Random Forest': RandomForestClassifier(n_estimators=100, random_state=42),
        'Logistic Regression': LogisticRegression(max_iter=1000, random_state=42),
        'Linear SVM': SVC(kernel='linear', probability=True, random_state=42),
        'XGBoost': XGBClassifier(eval_metric='mlogloss', random_state=42),
        'LightGBM': LGBMClassifier(random_state=42, verbose=-1),
        'CatBoost': CatBoostClassifier(verbose=0, random_state=42)
    }

    best_model = None
    best_model_name = ""
    best_f1 = -1.0
    results = {}

    for name, model in candidate_models.items():
        try:
            model.fit(X_train, y_train)
            y_pred = model.predict(X_test)

            acc = accuracy_score(y_test, y_pred)
            prec, rec, f1, _ = precision_recall_fscore_support(y_test, y_pred, average='weighted', zero_division=0)
            
            results[name] = {
                'Accuracy': acc,
                'Precision': prec,
                'Recall': rec,
                'F1': f1,
                'model': model
            }

            print(f"\nModel: {name}")
            print(f"Accuracy:  {acc:.4f}")
            print(f"Precision: {prec:.4f}")
            print(f"Recall:    {rec:.4f}")
            print(f"F1-Score:  {f1:.4f}")

            if f1 > best_f1:
                best_f1 = f1
                best_model_name = name
                best_model = model
        except Exception as e:
            print(f"\nModel: {name} - Skipped ({e})")

    print(f"\n==========================================")
    print(f"BEST MODEL SELECTED: {best_model_name} (F1: {best_f1:.4f})")
    print(f"==========================================")

    # Detailed Evaluation for Best Model
    y_best_pred = best_model.predict(X_test)
    target_names = [str(cls) for cls in label_encoder.classes_]
    
    print("\nClassification Report:")
    print(classification_report(y_test, y_best_pred, target_names=target_names, zero_division=0))

    print("\nConfusion Matrix:")
    print(confusion_matrix(y_test, y_best_pred))

    # Save artifacts: models/tfidf.pkl, models/label_encoder.pkl, models/model.pkl
    joblib.dump(tfidf, os.path.join(models_dir, 'tfidf.pkl'))
    joblib.dump(label_encoder, os.path.join(models_dir, 'label_encoder.pkl'))
    joblib.dump(best_model, os.path.join(models_dir, 'model.pkl'))
    print(f"\nSaved models to '{models_dir}/'")

if __name__ == '__main__':
    script_dir = os.path.dirname(os.path.abspath(__file__))
    data_path = os.path.join(script_dir, 'data.csv')
    if os.path.exists(data_path):
        data = load_data(data_path)
        train_and_evaluate(data)
    else:
        print(f"Dataset not found at {data_path}")
