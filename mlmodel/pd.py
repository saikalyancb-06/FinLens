import re
import pandas as pd
import pdfplumber


def extract_bank_statement_to_excel("", output_excel):
    # Standard headers for the Bank of Baroda statement format
    headers = [
        "TRAN DATE",
        "VALUE DATE",
        "NARRATION",
        "CHQ.NO.",
        "WITHDRAWAL(DR)",
        "DEPOSIT(CR)",
        "BALANCE(INR)",
    ]

    extracted_rows = []

    # Regex to recognize transaction dates (DD/MM/YYYY)
    date_pattern = re.compile(r"^\d{2}/\d{2}/\d{4}")

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue

            lines = text.split("\n")

            for line in lines:
                # Filter out headers, footers, page numbers, and metadata
                if any(
                    ignore in line
                    for ignore in [
                        "TRAN DATE",
                        "Statement of transactions",
                        "Bank of Baroda",
                        "Page ",
                        "This is computer-generated",
                        "Contact-Us",
                        "Main Account Holder",
                        "Customer Id:",
                    ]
                ):
                    continue

                # Split by pipe character used in layout delimiters
                parts = [p.strip() for p in line.split("|")]

                # Validate if line begins with a transaction date
                if len(parts) >= 2 and date_pattern.match(parts[0]):
                    # Ensure row padding aligns with the 7 expected columns
                    while len(parts) < len(headers):
                        parts.append("")
                    extracted_rows.append(parts[: len(headers)])

    # Convert to DataFrame and export to Excel
    df = pd.DataFrame(extracted_rows, columns=headers)

    # Save to Excel file
    with pd.ExcelWriter(output_excel, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Transactions")

    print(
        f"Extraction complete! Saved {len(extracted_rows)} records to {output_excel}"
    )


# Run extraction locally
extract_bank_statement_to_excel(
    "Current Account.pdf", "extracted_statement.xlsx"
)

# Absolute path example (Windows)
extract_bank_statement_to_excel(
    "Current Account.pdf", r"C:\Users\Saikalyan-Kredo\Documents\extracted_statement.xlsx"
)

# Absolute path example (Mac/Linux)
extract_bank_statement_to_excel(
    "Current Account.pdf",
    "/Users/Saikalyan-Kredo/Documents/extracted_statement.xlsx",
)