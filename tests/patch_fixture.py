# -*- coding: utf-8 -*-
content = open('tests/run_failure_fixtures.py', encoding='utf-8').read()

# Remove the block that creates a new 000455 bank txn -- the real PDF already has one
old = (
    '    # 3. cheque 000455: bank has ₹47,500, books record ₹82,000 → amount mismatch\n'
    '    # Tier-1 fires: same reference, different amount → PENDING_REVIEW.\n'
    '    # Both sides are consumed (matched_book_ids + consumed_bank_ids), so neither\n'
    '    # contributes to the BRS bridge. Residual stays 0.\n'
    '    # The bank transaction for 000455 already exists in the live DB (from the PDF).\n'
    '    existing_455 = db.query(Transaction).filter(\n'
    '        Transaction.user_id == user.id,\n'
    "        Transaction.reference_no == '000455'\n"
    '    ).first()\n'
    '    if not existing_455:\n'
    '        tx_455 = Transaction(\n'
    '            id=uuid.uuid4(),\n'
    '            user_id=user.id,\n'
    '            account_id=account.id,\n'
    '            txn_date=date(2025, 1, 24),\n'
    '            value_date=date(2025, 1, 24),\n'
    '            direction=Direction.DEBIT,\n'
    '            source_type=SourceType.STATEMENT,\n'
    "            narration_clean='CHQ PAID-CTS-000455',\n"
    "            reference_no='000455',\n"
    "            debit_paise='4750000',   # ₹47,500\n"
    "            credit_paise='0',\n"
    "            balance_paise='0',\n"
    '            superseded_by_id=None\n'
    '        )\n'
    '        db.add(tx_455)\n'
    '        print("  Created bank txn for cheque 000455 (\\u20b947,500).")\n'
    '    else:\n'
    '        print(f"  Bank txn for cheque 000455 already exists (debit={existing_455.debit_paise}).")\n'
    '    # Book entry for 000455 at ₹82,000 — intentionally wrong amount\n'
    '    make_book_entry(db, user.id, account.id, batch.id, date(2025, 1, 24),\n'
    '                    money_out=82000.00, instrument_no=\'000455\',\n'
    '                    narration=\'Chq 000455 to ABC Suppliers\')\n'
    '    print("  Added book entry for cheque 000455 at \\u20b982,000 (bank has \\u20b947,500) → PENDING_REVIEW.")'
)
new = (
    '    # 3. cheque 000455: the real bank statement has ₹47,500 for this cheque.\n'
    '    # We record it in books at ₹82,000 → Tier-1 fires PENDING_REVIEW (ref matches,\n'
    '    # amount differs). Both sides consumed → no BRS bridge delta → residual stays 0.\n'
    '    # The real PDF bank txn for 000455 already exists; do NOT create another one.\n'
    '    make_book_entry(db, user.id, account.id, batch.id, date(2025, 1, 24),\n'
    "                    money_out=82000.00, instrument_no='000455',\n"
    "                    narration='Chq 000455 to ABC Suppliers')\n"
    '    print("  Added book entry for cheque 000455 at 82000 (bank has 47500) PENDING_REVIEW.")'
)
assert old in content, 'Old pattern not found!'
content = content.replace(old, new, 1)
open('tests/run_failure_fixtures.py', 'w', encoding='utf-8').write(content)
print('Patched successfully')
