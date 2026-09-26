import pandas as pd
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import LabelEncoder
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.svm import SVC

# Load dataset
df = pd.read_csv('data.csv')

def map_target_category(row) -> str:
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

df['target_category'] = df.apply(map_target_category, axis=1)

clean_narrations = df['narration'].astype(str).str.lower().str.replace(r'[^\w\s]', ' ', regex=True)
amounts = np.maximum(df['withdrawal'].fillna(0), df['deposit'].fillna(0))
is_credits = (df['deposit'].fillna(0) > 0).astype(int)
is_debits = (df['withdrawal'].fillna(0) > 0).astype(int)

tfidf = TfidfVectorizer(max_features=5000, ngram_range=(1, 2))
X_tfidf = tfidf.fit_transform(clean_narrations).toarray()
X_num = np.column_stack([amounts, is_credits, is_debits])
X = np.hstack([X_tfidf, X_num])

label_encoder = LabelEncoder()
y = label_encoder.fit_transform(df['target_category'])

X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.20, random_state=42)

models = {
    'Random Forest': RandomForestClassifier(n_estimators=100, random_state=42),
    'Logistic Regression': LogisticRegression(max_iter=1000, random_state=42),
    'Linear SVM': SVC(kernel='linear', probability=True, random_state=42)
}

print("=== EVALUATION RESULTS ===")
for name, model in models.items():
    model.fit(X_train, y_train)
    acc = model.score(X_test, y_test)
    print(f"{name} Accuracy: {acc * 100:.2f}%")
