import os
import joblib
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.model_selection import train_test_split, GridSearchCV
from sklearn.metrics import accuracy_score, precision_recall_fscore_support, classification_report, confusion_matrix

from xgboost import XGBClassifier
from lightgbm import LGBMClassifier
from catboost import CatBoostClassifier

from preprocessing import clean_text, extract_derived_features
from rules import RuleEngine

def load_and_preprocess_data(file_path: str = 'data.csv'):
    df = pd.read_csv(file_path)
    
    def map_target(row):
        nature = str(row.get('nature_of_transaction', '')).strip()
        head = str(row.get('head', '')).strip()
        narr = str(row.get('narration', '')).strip().upper()

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

    df['target_category'] = df.apply(map_target, axis=1)

    clean_narrations, amounts, is_credits, is_debits, targets = [], [], [], [], []

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

    scaler = StandardScaler()
    X_num = scaler.fit_transform(np.column_stack([amounts, is_credits, is_debits]))
    X = np.hstack([X_tfidf, X_num])

    label_encoder = LabelEncoder()
    y = label_encoder.fit_transform(targets)

    return X, y, tfidf, label_encoder, scaler

def benchmark_and_tune(X, y):
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.20, random_state=42, stratify=y if len(np.unique(y)) > 1 else None
    )

    print("==========================================")
    print("STAGE 4: HYPERPARAMETER TUNING & BENCHMARKING")
    print("==========================================")

    # 1. CatBoost Tuning
    print("\n--- Tuning CatBoost Classifier ---")
    cb_param_grid = {
        'iterations': [100, 200],
        'depth': [4, 6],
        'learning_rate': [0.03, 0.1]
    }
    cb = CatBoostClassifier(verbose=0, random_state=42)
    grid_cb = GridSearchCV(cb, cb_param_grid, cv=3, scoring='f1_weighted', n_jobs=-1)
    grid_cb.fit(X_train, y_train)
    best_cb = grid_cb.best_estimator_
    y_cb_pred = best_cb.predict(X_test)
    cb_f1 = precision_recall_fscore_support(y_test, y_cb_pred, average='weighted', zero_division=0)[2]
    cb_acc = accuracy_score(y_test, y_cb_pred)
    print(f"CatBoost Best Params: {grid_cb.best_params_}")
    print(f"CatBoost Accuracy: {cb_acc*100:.2f}% | Weighted F1: {cb_f1:.4f}")

    # 2. XGBoost Tuning
    print("\n--- Tuning XGBoost Classifier ---")
    xgb_param_grid = {
        'n_estimators': [100, 200],
        'max_depth': [3, 5],
        'learning_rate': [0.05, 0.1]
    }
    xgb = XGBClassifier(eval_metric='mlogloss', random_state=42)
    grid_xgb = GridSearchCV(xgb, xgb_param_grid, cv=3, scoring='f1_weighted', n_jobs=-1)
    grid_xgb.fit(X_train, y_train)
    best_xgb = grid_xgb.best_estimator_
    y_xgb_pred = best_xgb.predict(X_test)
    xgb_f1 = precision_recall_fscore_support(y_test, y_xgb_pred, average='weighted', zero_division=0)[2]
    xgb_acc = accuracy_score(y_test, y_xgb_pred)
    print(f"XGBoost Best Params: {grid_xgb.best_params_}")
    print(f"XGBoost Accuracy: {xgb_acc*100:.2f}% | Weighted F1: {xgb_f1:.4f}")

    # 3. LightGBM Tuning
    print("\n--- Tuning LightGBM Classifier ---")
    lgb_param_grid = {
        'n_estimators': [100, 200],
        'num_leaves': [15, 31],
        'learning_rate': [0.05, 0.1]
    }
    lgb = LGBMClassifier(random_state=42, verbose=-1)
    grid_lgb = GridSearchCV(lgb, lgb_param_grid, cv=3, scoring='f1_weighted', n_jobs=-1)
    grid_lgb.fit(X_train, y_train)
    best_lgb = grid_lgb.best_estimator_
    y_lgb_pred = best_lgb.predict(X_test)
    lgb_f1 = precision_recall_fscore_support(y_test, y_lgb_pred, average='weighted', zero_division=0)[2]
    lgb_acc = accuracy_score(y_test, y_lgb_pred)
    print(f"LightGBM Best Params: {grid_lgb.best_params_}")
    print(f"LightGBM Accuracy: {lgb_acc*100:.2f}% | Weighted F1: {lgb_f1:.4f}")

    models_res = {
        'CatBoost': (best_cb, cb_f1, cb_acc),
        'XGBoost': (best_xgb, xgb_f1, xgb_acc),
        'LightGBM': (best_lgb, lgb_f1, lgb_acc)
    }

    best_name = max(models_res, key=lambda k: models_res[k][1])
    winning_model, winning_f1, winning_acc = models_res[best_name]

    print("\n==========================================")
    print(f"WINNING MODEL: {best_name} (Accuracy: {winning_acc*100:.2f}%, F1: {winning_f1:.4f})")
    print("==========================================")

    return winning_model, best_name

if __name__ == '__main__':
    X, y, tfidf, label_encoder, scaler = load_and_preprocess_data('data.csv')
    best_model, best_name = benchmark_and_tune(X, y)

    os.makedirs('models', exist_ok=True)
    joblib.dump(tfidf, 'models/tfidf.pkl')
    joblib.dump(label_encoder, 'models/label_encoder.pkl')
    joblib.dump(best_model, 'models/model.pkl')
    print(f"\nSaved best tuned model ({best_name}) to 'models/' directory.")
