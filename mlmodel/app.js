// Seeded Demo Data (Matching exact values from provided UI mockup image)
const INITIAL_TRANSACTIONS = [
    { id: 1, date: "2026-01-30", entity: "ACME Manufacturing", bank: "HDFC Bank", type: "Outflow", narration: "Payment to Steel Suppliers Ltd", amount: -245000, category: "vendor", confidence: 92 },
    { id: 2, date: "2026-01-30", entity: "ACME Retail", bank: "ICICI Bank", type: "Inflow", narration: "Customer payment - Invoice #12345", amount: 180000, category: "customer", confidence: 95 },
    { id: 3, date: "2026-01-29", entity: "GlobalCorp APAC", bank: "Axis Bank", type: "Outflow", narration: "Monthly salary - January 2026", amount: -1850000, category: "salary", confidence: 98 },
    { id: 4, date: "2026-01-29", entity: "ACME Logistics", bank: "SBI", type: "Outflow", narration: "GST Payment - Dec 2025", amount: -385000, category: "statutory", confidence: 97 },
    { id: 5, date: "2026-01-28", entity: "ACME Manufacturing", bank: "HDFC Bank", type: "Outflow", narration: "Office rent - January 2026", amount: -125000, category: "rent", confidence: 99 },
    { id: 6, date: "2026-01-28", entity: "GlobalCorp EMEA", bank: "Kotak Mahindra", type: "Outflow", narration: "Bank service charges", amount: -2500, category: "bank-charges", confidence: 100 },
    { id: 7, date: "2026-01-27", entity: "ACME Retail", bank: "ICICI Bank", type: "Inflow", narration: "Export proceeds - USD conversion", amount: 4250000, category: "customer", confidence: 88 },
    { id: 8, date: "2026-01-27", entity: "GlobalCorp Americas", bank: "Citi Bank", type: "Outflow", narration: "Interest on overdraft", amount: -15000, category: "interest", confidence: 100 },
    { id: 9, date: "2026-01-26", entity: "Resilient Innovations", bank: "State Bank", type: "Inflow", narration: "NEFT-YESAP50900722527-RESILIENT INNOVATIONS PVT LTD", amount: 30078, category: "Settlement", confidence: 100 },
    { id: 10, date: "2026-01-25", entity: "Swiggy India", bank: "HDFC Bank", type: "Outflow", narration: "SWIGGY BANGALORE IN ORDER #8821", amount: -450, category: "Food", confidence: 100 },
    { id: 11, date: "2026-01-24", entity: "Indian Oil Corp", bank: "ICICI Bank", type: "Outflow", narration: "INDIAN OIL PETROL PUMP MUMBAI", amount: -2500, category: "Fuel", confidence: 100 },
    { id: 12, date: "2026-01-23", entity: "Unknown Entity", bank: "Axis Bank", type: "Outflow", narration: "MISC SUPPLIES REFUND TRANSFER", amount: -12000, category: "Unknown", confidence: 64 },
    { id: 13, date: "2026-01-22", entity: "Unknown Entity", bank: "SBI", type: "Inflow", narration: "DIRECT REMITTANCE REF 99018", amount: 5500, category: "Unknown", confidence: 71 },
    { id: 14, date: "2026-01-21", entity: "Unknown Entity", bank: "HDFC Bank", type: "Outflow", narration: "VENDOR ADJUSTMENT CHARGE", amount: -8500, category: "Unknown", confidence: 55 },
    { id: 15, date: "2026-01-20", entity: "BESCOM Electric", bank: "Canara Bank", type: "Outflow", narration: "BESCOM UTILITY BILL PAYMENT", amount: -45000, category: "Utilities", confidence: 100 }
];

let transactions = [];
let activeEditId = null;

// Initialize App
document.addEventListener("DOMContentLoaded", () => {
    loadTransactionsFromStorage();
    renderDashboard();
});

function loadTransactionsFromStorage() {
    const saved = localStorage.getItem("erp_transactions");
    if (saved) {
        transactions = JSON.parse(saved);
    } else {
        transactions = JSON.parse(JSON.stringify(INITIAL_TRANSACTIONS));
        localStorage.setItem("erp_transactions", JSON.stringify(transactions));
    }
}

function saveTransactionsToStorage() {
    localStorage.setItem("erp_transactions", JSON.stringify(transactions));
}

// Formatting Helper Rules:
// Negative numbers: shown in red brackets e.g. (12.50) — never with a minus sign
function formatAmount(amount) {
    const isNegative = amount < 0;
    const absVal = Math.abs(amount);
    const formattedStr = "₹" + absVal.toLocaleString('en-IN');

    if (isNegative) {
        return `<span class="mono text-red">(${formattedStr.replace('₹', '₹')})</span>`;
    }
    return `<span class="mono">${formattedStr}</span>`;
}

function renderDashboard(dataToRender = null) {
    const data = dataToRender || transactions;
    const tbody = document.getElementById("transaction-rows");
    tbody.innerHTML = "";

    data.forEach(tx => {
        const row = document.createElement("tr");
        
        const isUnknown = tx.category.toLowerCase() === "unknown";
        const catBadgeClass = isUnknown ? "category-tag tag-unknown" : "category-tag";

        row.innerHTML = `
            <td class="mono">${tx.date}</td>
            <td>${tx.entity}</td>
            <td>${tx.bank}</td>
            <td><span class="type-badge">${tx.type}</span></td>
            <td>${tx.narration}</td>
            <td class="text-right">${formatAmount(tx.amount)}</td>
            <td><span class="${catBadgeClass}">${tx.category}</span></td>
            <td class="text-right mono">${tx.confidence}%</td>
            <td class="text-center">
                <button class="btn btn-secondary" onclick="openEditModal(${tx.id})">Edit</button>
            </td>
        `;
        tbody.appendChild(row);
    });

    updateMetrics();
}

function updateMetrics() {
    const total = transactions.length;
    const categorized = transactions.filter(t => t.category.toLowerCase() !== 'unknown').length;
    const uncategorized = total - categorized;

    const sumConf = transactions.reduce((acc, t) => acc + t.confidence, 0);
    const avgConf = total > 0 ? Math.round(sumConf / total) : 0;

    document.getElementById("metric-total").innerText = total;
    document.getElementById("metric-categorized").innerText = categorized;
    document.getElementById("metric-uncategorized").innerText = uncategorized;
    document.getElementById("metric-avg-confidence").innerText = `${avgConf}%`;
}

// Filter Logic
function applyFilters() {
    const flow = document.getElementById("filter-flow").value;
    const cat = document.getElementById("filter-category").value.toLowerCase();
    const minAmt = parseFloat(document.getElementById("filter-min-amount").value);
    const maxAmt = parseFloat(document.getElementById("filter-max-amount").value);

    let filtered = transactions.filter(t => {
        if (flow !== "All" && t.type !== flow) return false;
        if (cat !== "all" && t.category.toLowerCase() !== cat) return false;
        
        const absAmount = Math.abs(t.amount);
        if (!isNaN(minAmt) && absAmount < minAmt) return false;
        if (!isNaN(maxAmt) && absAmount > maxAmt) return false;

        return true;
    });

    renderDashboard(filtered);
}

function resetFilters() {
    document.getElementById("filter-flow").value = "All";
    document.getElementById("filter-category").value = "All";
    document.getElementById("filter-min-amount").value = "";
    document.getElementById("filter-max-amount").value = "";
    renderDashboard();
}

// Seed / Drop DB Handlers
function seedDatabase() {
    transactions = JSON.parse(JSON.stringify(INITIAL_TRANSACTIONS));
    saveTransactionsToStorage();
    renderDashboard();
    alert("Database re-seeded with demo data.");
}

function resetDatabase() {
    if (confirm("Are you sure you want to drop and reset the database?")) {
        transactions = [];
        saveTransactionsToStorage();
        renderDashboard();
    }
}

// Edit Modal Handler
function openEditModal(id) {
    const tx = transactions.find(t => t.id === id);
    if (!tx) return;

    activeEditId = id;
    document.getElementById("modal-narration").innerText = tx.narration;
    document.getElementById("modal-amount").innerHTML = formatAmount(tx.amount);
    document.getElementById("modal-category-select").value = tx.category;

    document.getElementById("edit-modal").style.display = "flex";
}

function saveCategoryEdit() {
    if (!activeEditId) return;
    const newCat = document.getElementById("modal-category-select").value;
    const tx = transactions.find(t => t.id === activeEditId);
    if (tx) {
        tx.category = newCat;
        tx.confidence = 100; // Manual user override gives 100% confidence
        saveTransactionsToStorage();
        renderDashboard();
    }
    closeModal("edit-modal");
}

function closeModal(modalId) {
    document.getElementById(modalId).style.display = "none";
}

// Test Prediction Functionality (Rule Engine + ML Simulation)
function openTestInputModal() {
    document.getElementById("test-narration").value = "";
    document.getElementById("test-withdrawal").value = "";
    document.getElementById("test-deposit").value = "";
    document.getElementById("test-result").style.display = "none";
    document.getElementById("test-modal").style.display = "flex";
}

function executeTestPredict() {
    const narr = document.getElementById("test-narration").value.trim();
    const w = parseFloat(document.getElementById("test-withdrawal").value) || 0;
    const d = parseFloat(document.getElementById("test-deposit").value) || 0;

    if (!narr) {
        alert("Please enter a narration string.");
        return;
    }

    const narrLower = narr.toLowerCase();
    let category = "Unknown";
    let method = "ML Model (Confidence < 80%)";
    let confidence = 72;

    // Simulate Rule Engine Execution
    if (narrLower.includes("swiggy") || narrLower.includes("zomato")) {
        category = "Food"; method = "Rule 10/11"; confidence = 100;
    } else if (narrLower.includes("bescom")) {
        category = "Utilities"; method = "Rule 17"; confidence = 100;
    } else if (narrLower.includes("amazon") || narrLower.includes("flipkart")) {
        category = "Shopping"; method = "Rule 14/15"; confidence = 100;
    } else if (narrLower.includes("resilient innovations")) {
        category = "Settlement"; method = "Rule 19"; confidence = 100;
    } else if (narrLower.includes("salary")) {
        category = "Salary"; method = "Rule 6"; confidence = 100;
    } else if (narrLower.includes("ebank:self")) {
        category = "Self Transfer"; method = "Rule 2"; confidence = 100;
    } else {
        // High confidence ML simulation
        category = "Vendor Payment";
        method = "Random Forest ML Model";
        confidence = 94;
    }

    // Add to table list
    const newTx = {
        id: Date.now(),
        date: new Date().toISOString().split('T')[0],
        entity: "Test Entry",
        bank: "Test Bank",
        type: w > 0 ? "Outflow" : "Inflow",
        narration: narr,
        amount: w > 0 ? -w : d,
        category: category,
        confidence: confidence
    };

    transactions.unshift(newTx);
    saveTransactionsToStorage();

    const resultBox = document.getElementById("test-result");
    resultBox.style.display = "block";
    resultBox.innerHTML = `
        <strong>Result:</strong> Category = <u>${category}</u><br>
        <strong>Confidence:</strong> ${confidence}%<br>
        <strong>Matched Engine:</strong> ${method}
    `;

    renderDashboard();
}

function exportCSV() {
    let csvContent = "data:text/csv;charset=utf-8,Date,Entity,Bank,Type,Narration,Amount,Category,Confidence\n";
    transactions.forEach(t => {
        csvContent += `${t.date},"${t.entity}","${t.bank}",${t.type},"${t.narration}",${t.amount},${t.category},${t.confidence}%\n`;
    });
    const encodedUri = encodeURI(csvContent);
    const link = document.createElement("a");
    link.setAttribute("href", encodedUri);
    link.setAttribute("download", "transactions_categorized.csv");
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
}
