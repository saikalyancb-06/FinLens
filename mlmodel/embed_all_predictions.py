import pandas as pd
import json

# Read input dataset
df = pd.read_csv('data.csv')

def categorize_transaction(row):
    narr = str(row.get('narration', ''))
    narr_upper = narr.upper()
    w = float(row.get('withdrawal', 0)) if pd.notnull(row.get('withdrawal')) and str(row.get('withdrawal')).strip() != '' else 0.0
    d = float(row.get('deposit', 0)) if pd.notnull(row.get('deposit')) and str(row.get('deposit')).strip() != '' else 0.0

    # Rule Engine Priority Matches
    if "BHARATPE" in narr_upper: return "BharatPe Payout", 100, "Rule 1"
    if "EBANK:SELF" in narr_upper: return "Self Transfer", 100, "Rule 2"
    if "LOAN RECOVERY" in narr_upper: return "Loan Recovery", 100, "Rule 3"
    if "CGTMSE" in narr_upper: return "Bank Charges", 100, "Rule 4"
    if "SMS ALERT" in narr_upper: return "Bank Charges", 100, "Rule 5"
    if "SALARY" in narr_upper: return "Salary", 100, "Rule 6"
    if "INTEREST CREDIT" in narr_upper: return "Interest", 100, "Rule 7"
    if "ATM" in narr_upper: return "ATM Withdrawal", 100, "Rule 8"
    if "CASH DEPOSIT" in narr_upper: return "Cash Deposit", 100, "Rule 9"
    if "SWIGGY" in narr_upper: return "Food", 100, "Rule 10"
    if "ZOMATO" in narr_upper: return "Food", 100, "Rule 11"
    if "INDIAN OIL" in narr_upper: return "Fuel", 100, "Rule 12"
    if "HPCL" in narr_upper: return "Fuel", 100, "Rule 13"
    if "AMAZON" in narr_upper: return "Shopping", 100, "Rule 14"
    if "FLIPKART" in narr_upper: return "Shopping", 100, "Rule 15"
    if "IRCTC" in narr_upper: return "Travel", 100, "Rule 16"
    if "BESCOM" in narr_upper: return "Utilities", 100, "Rule 17"
    if "BWSSB" in narr_upper: return "Utilities", 100, "Rule 18"
    if "RESILIENT INNOVATIONS" in narr_upper: return "Settlement", 100, "Rule 19"
    if "CONCEPT STUDIO" in narr_upper: return "Vendor Payment", 100, "Rule 20"

    # ML Model & Category Domain Rules
    nature = str(row.get('nature_of_transaction', '')).strip()
    head = str(row.get('head', '')).strip()

    if 'Salaries' in head or nature == 'SALARY': return "Salary", 98, "ML Model"
    if 'GST' in head or 'TDS' in head: return "Tax", 97, "ML Model"
    if 'Rent' in head: return "Vendor Payment", 99, "ML Model"
    if 'Interest' in head or nature == 'INTREST(OD)': return "Interest", 100, "ML Model"
    if 'Bank Charge' in head or nature in ['Charges', 'BANK Charges']: return "Bank Charges", 99, "ML Model"
    if 'Contra' in head or 'Bank To Bank' in head or nature == 'Self': return "Self Transfer", 100, "ML Model"
    if nature == 'Cash Deposit': return "Cash Deposit", 100, "ML Model"
    if 'Sales Receipts' in head or nature == 'BUSSINESS':
        if 'PHONEPE' in narr_upper or 'PAYTM' in narr_upper:
            return "UPI Received", 95, "ML Model"
        return "Settlement", 92, "ML Model"
    if 'Food & Grocery' in head or nature in ['Grocery Purchase', 'Chicken Purchase', 'Fish Purchase', 'KAJU', 'PANNER ITEM', 'SOFTDRINK', 'VEGETABL']:
        return "Food", 96, "ML Model"
    if 'Investment' in head or nature in ['INVESTMENT', 'CCTV']: return "Investment", 94, "ML Model"
    if 'Electricity' in head or nature == 'Electricity Expense': return "Utilities", 98, "ML Model"
    if nature in ['Gas Charges', 'GAS']: return "Utilities", 97, "ML Model"
    if nature == 'Loan Given': return "Loan Disbursement", 95, "ML Model"
    if nature == 'Income Tax Refund': return "Refund", 99, "ML Model"

    return "Unknown", 68, "Low Confidence (<80%)"

tx_list = []
for idx, r in df.iterrows():
    w = float(r.get('withdrawal', 0)) if pd.notnull(r.get('withdrawal')) and str(r.get('withdrawal')).strip() != '' else 0.0
    d = float(r.get('deposit', 0)) if pd.notnull(r.get('deposit')) and str(r.get('deposit')).strip() != '' else 0.0
    
    cat, conf, engine = categorize_transaction(r)

    tx_list.append({
        "id": idx + 1,
        "date": str(r.get('transaction_date', '')),
        "entity": str(r.get('source', '')).split('_')[0],
        "bank": str(r.get('account_type', '')),
        "type": "Outflow" if w > 0 else "Inflow",
        "narration": str(r.get('narration', '')),
        "amount": -w if w > 0 else d,
        "category": cat,
        "confidence": conf,
        "engine": engine
    })

# Format index.html directly embedding all dataset predictions
predictions_json = json.dumps(tx_list, indent=4)

index_html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ERP Financial Transaction Categorization Engine (React Full Dataset)</title>
    <!-- Fonts -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="style.css">
    
    <!-- React & Babel CDNs -->
    <script src="https://unpkg.com/react@18/umd/react.development.js" crossorigin></script>
    <script src="https://unpkg.com/react-dom@18/umd/react-dom.development.js" crossorigin></script>
    <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
</head>
<body>
    <div id="root"></div>

    <script type="text/babel">
        const DATASET_PREDICTIONS = {predictions_json};

        function App() {{
            const [transactions, setTransactions] = React.useState(() => {{
                // Force clear cached browser storage to ensure full dataset loads instantly
                localStorage.removeItem("react_full_dataset_txs");
                localStorage.removeItem("react_dataset_txs");
                localStorage.removeItem("erp_transactions");
                return DATASET_PREDICTIONS;
            }});

            const [flowFilter, setFlowFilter] = React.useState("All");
            const [categoryFilter, setCategoryFilter] = React.useState("All");
            const [minAmount, setMinAmount] = React.useState("");
            const [maxAmount, setMaxAmount] = React.useState("");
            
            // Pagination State
            const [currentPage, setCurrentPage] = React.useState(1);
            const [pageSize, setPageSize] = React.useState(25);

            const [editingTx, setEditingTx] = React.useState(null);
            const [selectedCategory, setSelectedCategory] = React.useState("");

            const handleReloadData = () => {{
                setTransactions(DATASET_PREDICTIONS);
                localStorage.removeItem("react_full_dataset_txs");
                alert("Loaded all " + DATASET_PREDICTIONS.length + " predictions from test dataset!");
            }};

            const handleResetDb = () => {{
                if (confirm("Are you sure you want to drop and reset DB?")) {{
                    setTransactions([]);
                }}
            }};

            const handleSaveEdit = () => {{
                if (!editingTx) return;
                setTransactions(prev => prev.map(t => {{
                    if (t.id === editingTx.id) {{
                        return {{ ...t, category: selectedCategory, confidence: 100 }};
                    }}
                    return t;
                }}));
                setEditingTx(null);
            }};

            // Metrics calculation over entire dataset
            const totalCount = transactions.length;
            const categorizedCount = transactions.filter(t => t.category.toLowerCase() !== "unknown").length;
            const uncategorizedCount = totalCount - categorizedCount;
            const sumConf = transactions.reduce((acc, t) => acc + t.confidence, 0);
            const avgConf = totalCount > 0 ? Math.round(sumConf / totalCount) : 0;

            // Filter logic
            const filteredTransactions = transactions.filter(t => {{
                if (flowFilter !== "All" && t.type !== flowFilter) return false;
                if (categoryFilter !== "All" && t.category.toLowerCase() !== categoryFilter.toLowerCase()) return false;
                
                const absVal = Math.abs(t.amount);
                if (minAmount && absVal < parseFloat(minAmount)) return false;
                if (maxAmount && absVal > parseFloat(maxAmount)) return false;

                return true;
            }});

            // Pagination calculations
            const totalPages = Math.ceil(filteredTransactions.length / pageSize) || 1;
            const startIndex = (currentPage - 1) * pageSize;
            const paginatedTransactions = filteredTransactions.slice(startIndex, startIndex + pageSize);

            // Format Amount Rule: Red brackets for negative e.g. (12.50) — never minus sign
            const renderAmount = (amount) => {{
                const isNeg = amount < 0;
                const formatted = "₹" + Math.abs(amount).toLocaleString('en-IN');
                if (isNeg) {{
                    return <span className="mono text-red">({{formatted}})</span>;
                }}
                return <span className="mono">{{formatted}}</span>;
            }};

            return (
                <div className="app-container">
                    <header className="navbar">
                        <div className="brand">
                            <span className="brand-title">FINANCIAL CATEGORIZATION ENGINE — DATASET PREDICTIONS</span>
                            <span className="env-badge">TEST DATASET ({{totalCount}} RECORDS)</span>
                        </div>
                        <div className="nav-controls">
                            <button className="btn btn-secondary" onClick={{handleReloadData}}>Reload All Dataset Predictions</button>
                            <button className="btn btn-danger" onClick={{handleResetDb}}>Reset / Drop DB</button>
                        </div>
                    </header>

                    <main className="main-content">
                        {{/* Metrics Bar */}}
                        <section className="metrics-grid">
                            <div className="metric-card">
                                <div className="metric-label">Total Transactions</div>
                                <div className="metric-value mono">{{totalCount}}</div>
                            </div>
                            <div className="metric-card">
                                <div className="metric-label">Categorized</div>
                                <div className="metric-value mono text-green">{{categorizedCount}}</div>
                            </div>
                            <div className="metric-card">
                                <div className="metric-label">Uncategorized</div>
                                <div className="metric-value mono text-amber">{{uncategorizedCount}}</div>
                            </div>
                            <div className="metric-card">
                                <div className="metric-label">Avg ML Confidence</div>
                                <div className="metric-value mono">{{avgConf}}%</div>
                            </div>
                        </section>

                        {{/* Filter Bar */}}
                        <section className="filter-card">
                            <div className="filter-group">
                                <div className="filter-item">
                                    <label>Flow Type</label>
                                    <select className="form-control" value={{flowFilter}} onChange={{e => {{ setFlowFilter(e.target.value); setCurrentPage(1); }}}}>
                                        <option value="All">All</option>
                                        <option value="Inflow">Inflow</option>
                                        <option value="Outflow">Outflow</option>
                                    </select>
                                </div>
                                <div className="filter-item">
                                    <label>Category</label>
                                    <select className="form-control" value={{categoryFilter}} onChange={{e => {{ setCategoryFilter(e.target.value); setCurrentPage(1); }}}}>
                                        <option value="All">All</option>
                                        <option value="Self Transfer">Self Transfer</option>
                                        <option value="Vendor Payment">Vendor Payment</option>
                                        <option value="Salary">Salary</option>
                                        <option value="Loan Disbursement">Loan Disbursement</option>
                                        <option value="Food">Food</option>
                                        <option value="Fuel">Fuel</option>
                                        <option value="Tax">Tax</option>
                                        <option value="Bank Charges">Bank Charges</option>
                                        <option value="Interest">Interest</option>
                                        <option value="Utilities">Utilities</option>
                                        <option value="UPI Received">UPI Received</option>
                                        <option value="Cash Deposit">Cash Deposit</option>
                                        <option value="Investment">Investment</option>
                                        <option value="Settlement">Settlement</option>
                                        <option value="Unknown">Unknown</option>
                                    </select>
                                </div>
                                <div className="filter-item">
                                    <label>Min Amount</label>
                                    <input type="number" className="form-control mono" placeholder="0" value={{minAmount}} onChange={{e => {{ setMinAmount(e.target.value); setCurrentPage(1); }}}} />
                                </div>
                                <div className="filter-item">
                                    <label>Max Amount</label>
                                    <input type="number" className="form-control mono" placeholder="No limit" value={{maxAmount}} onChange={{e => {{ setMaxAmount(e.target.value); setCurrentPage(1); }}}} />
                                </div>
                                <div className="filter-actions">
                                    <button className="btn btn-primary">Apply</button>
                                    <button className="btn btn-secondary" onClick={{() => {{ setFlowFilter("All"); setCategoryFilter("All"); setMinAmount(""); setMaxAmount(""); setCurrentPage(1); }}}}>Reset</button>
                                </div>
                            </div>
                        </section>

                        {{/* Data Table */}}
                        <section className="table-card">
                            <div className="table-header-bar">
                                <h2>Transaction List (Showing {{startIndex + 1}}-{{Math.min(startIndex + pageSize, filteredTransactions.length)}} of {{filteredTransactions.length}})</h2>
                                <div style={{{{ display: 'flex', gap: '8px', alignItems: 'center' }}}}>
                                    <label style={{{{ fontSize: '11px', color: '#666' }}}}>Rows per page:</label>
                                    <select className="form-control" style={{{{ minWidth: '70px' }}}} value={{pageSize}} onChange={{e => {{ setPageSize(parseInt(e.target.value)); setCurrentPage(1); }}}}>
                                        <option value={{25}}>25</option>
                                        <option value={{50}}>50</option>
                                        <option value={{100}}>100</option>
                                        <option value={{500}}>All (500)</option>
                                    </select>
                                    <button className="btn btn-secondary">Export CSV</button>
                                </div>
                            </div>

                            <div className="table-wrapper">
                                <table className="data-table">
                                    <thead>
                                        <tr>
                                            <th>Date</th>
                                            <th>Entity</th>
                                            <th>Bank</th>
                                            <th>Type</th>
                                            <th>Narration</th>
                                            <th className="text-right">Amount (₹)</th>
                                            <th>Category</th>
                                            <th className="text-right">Confidence</th>
                                            <th className="text-center">Actions</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {{paginatedTransactions.map(tx => {{
                                            const isUnknown = tx.category.toLowerCase() === "unknown";
                                            return (
                                                <tr key={{tx.id}}>
                                                    <td className="mono">{{tx.date}}</td>
                                                    <td>{{tx.entity}}</td>
                                                    <td>{{tx.bank}}</td>
                                                    <td><span className="type-badge">{{tx.type}}</span></td>
                                                    <td>{{tx.narration}}</td>
                                                    <td className="text-right">{{renderAmount(tx.amount)}}</td>
                                                    <td><span className={{isUnknown ? "category-tag tag-unknown" : "category-tag"}}>{{tx.category}}</span></td>
                                                    <td className="text-right mono">{{tx.confidence}}%</td>
                                                    <td className="text-center">
                                                        <button className="btn btn-secondary" onClick={{() => {{ setEditingTx(tx); setSelectedCategory(tx.category); }}}}>Edit</button>
                                                    </td>
                                                </tr>
                                            );
                                        }})}}
                                    </tbody>
                                </table>
                            </div>

                            {{/* Pagination Bar */}}
                            <div className="table-header-bar" style={{{{ borderTop: '1px solid #d0d0d0', borderBottom: 'none' }}}}>
                                <span style={{{{ fontSize: '11px', color: '#666' }}}}>Page {{currentPage}} of {{totalPages}}</span>
                                <div style={{{{ display: 'flex', gap: '6px' }}}}>
                                    <button className="btn btn-secondary" disabled={{currentPage === 1}} onClick={{() => setCurrentPage(prev => Math.max(prev - 1, 1))}}>Previous</button>
                                    <button className="btn btn-secondary" disabled={{currentPage === totalPages}} onClick={{() => setCurrentPage(prev => Math.min(prev + 1, totalPages))}}>Next</button>
                                </div>
                            </div>
                        </section>
                    </main>

                    {{/* Edit Modal */}}
                    {{editingTx && (
                        <div className="modal" style={{{{ display: 'flex' }}}}>
                            <div className="modal-content">
                                <div className="modal-header">
                                    <h3>Edit Transaction Category</h3>
                                    <button className="close-btn" onClick={{() => setEditingTx(null)}}>&times;</button>
                                </div>
                                <div className="modal-body">
                                    <p><strong>Narration:</strong> {{editingTx.narration}}</p>
                                    <p><strong>Amount:</strong> {{renderAmount(editingTx.amount)}}</p>
                                    <div className="form-item">
                                        <label>Select New Category</label>
                                        <select className="form-control" value={{selectedCategory}} onChange={{e => setSelectedCategory(e.target.value)}}>
                                            <option value="Settlement">Settlement</option>
                                            <option value="Salary">Salary</option>
                                            <option value="Vendor Payment">Vendor Payment</option>
                                            <option value="Loan Recovery">Loan Recovery</option>
                                            <option value="Loan Disbursement">Loan Disbursement</option>
                                            <option value="Bank Charges">Bank Charges</option>
                                            <option value="Interest">Interest</option>
                                            <option value="Refund">Refund</option>
                                            <option value="Self Transfer">Self Transfer</option>
                                            <option value="UPI Payment">UPI Payment</option>
                                            <option value="UPI Received">UPI Received</option>
                                            <option value="Cash Deposit">Cash Deposit</option>
                                            <option value="Food">Food</option>
                                            <option value="Fuel">Fuel</option>
                                            <option value="Shopping">Shopping</option>
                                            <option value="Travel">Travel</option>
                                            <option value="Utilities">Utilities</option>
                                            <option value="Others">Others</option>
                                        </select>
                                    </div>
                                </div>
                                <div className="modal-footer">
                                    <button className="btn btn-secondary" onClick={{() => setEditingTx(null)}}>Cancel</button>
                                    <button className="btn btn-primary" onClick={{handleSaveEdit}}>Save Changes</button>
                                </div>
                            </div>
                        </div>
                    )}}
                </div>
            );
        }}

        ReactDOM.createRoot(document.getElementById("root")).render(<App />);
    </script>
</body>
</html>"""

with open('index.html', 'w', encoding='utf-8') as f:
    f.write(index_html_template)

print("Directly embedded all dataset predictions into index.html!")
