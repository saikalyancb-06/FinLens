import uuid
import logging
import datetime
from typing import Dict, Any, List

from sqlalchemy.orm import Session

from app.ai.decision_engine import HybridDecisionEngine
from app.services.transaction_storage import TransactionStorageService

logger = logging.getLogger(__name__)

class AccountAggregatorService:
    """Service to process, normalize, categorize, and persist Account Aggregator (AA) FI data."""

    def __init__(self):
        self.decision_engine = HybridDecisionEngine()
        self.storage_service = TransactionStorageService()

    def process_and_store_fi_data(
        self,
        db: Session,
        user_id: str,
        fi_payload: Dict[str, Any]
    ) -> List[Any]:
        """Normalizes ReBIT JSON FI data, evaluates with Rule+ML engine, and stores in database."""
        raw_txns = self._extract_transactions(fi_payload)
        if not raw_txns:
            logger.info("[AA Service] No transactions found in FI payload.")
            return []

        normalized_txns = []
        for item in raw_txns:
            norm = self._normalize_transaction(item)
            if norm:
                normalized_txns.append(norm)

        if not normalized_txns:
            return []

        # 1. Categorize via Rule+ML Engine
        evaluated_txns = self.decision_engine.evaluate_transactions(normalized_txns)

        # 2. Persist into ProcessedTransaction table (Staging Layer)
        stored_records = self.storage_service.store_processed_transactions(
            db=db,
            processed_transactions=evaluated_txns,
            user_id=user_id
        )

        # 3. Persist into canonical Transaction table
        from app.models.account import Account
        from app.services.transaction_storage import resolve_account_id

        user_uuid = uuid.UUID(str(user_id)) if isinstance(user_id, str) else user_id
        # Was `.filter(Account.user_id == user_uuid).first()` — an arbitrary pick
        # that also matched soft-deleted accounts, so a user with several accounts
        # could have AA transactions filed against the wrong one. resolve_account_id
        # returns None rather than guessing when the choice is ambiguous.
        acct_id = resolve_account_id(db, user_uuid, evaluated_txns)
        user_acct = db.query(Account).filter(Account.id == acct_id).first() if acct_id else None
        ent_id = user_acct.entity_id if user_acct else None

        self.storage_service.store_transactions(
            db=db,
            processed_txns=evaluated_txns,
            user_id=user_uuid,
            account_id=acct_id,
            entity_id=ent_id,
            source_channel="ACCOUNT_AGGREGATOR"
        )

        logger.info(f"[AA Service] Successfully categorized and persisted {len(stored_records)} Account Aggregator transactions.")
        return stored_records


    def _extract_transactions(self, fi_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        extracted = []
        fi_list = fi_payload.get("FI", [])
        for fi in fi_list:
            fip_id = fi.get("fipID", "AA Bank")
            data_list = fi.get("data", [])
            for d in data_list:
                content = d.get("decryptedContent", {})
                account = content.get("Account", {})
                masked_acc = account.get("maskedAccNumber", "")
                acc_last4 = masked_acc[-4:] if len(masked_acc) >= 4 else ""
                
                txns_node = account.get("Transactions", {})
                txn_list = txns_node.get("transaction", [])
                if isinstance(txn_list, dict):
                    txn_list = [txn_list]

                for t in txn_list:
                    t["_fip_id"] = fip_id
                    t["_account_last4"] = acc_last4
                    extracted.append(t)
        return extracted

    def _normalize_transaction(self, raw_txn: Dict[str, Any]) -> Dict[str, Any]:
        try:
            amount_val = float(raw_txn.get("amount", 0.0))
            txn_type = str(raw_txn.get("type", "DEBIT")).upper()
            debit_val = amount_val if txn_type in ("DEBIT", "DR", "OUTFLOW") else 0.0
            credit_val = amount_val if txn_type in ("CREDIT", "CR", "INFLOW") else 0.0

            bal_val = float(raw_txn.get("currentBalance", 0.0))
            mode = raw_txn.get("mode", "ONLINE")
            ref_no = raw_txn.get("reference") or raw_txn.get("txnId") or ""
            desc = raw_txn.get("narration") or f"AA Transfer {ref_no}"
            
            raw_ts = raw_txn.get("transactionTimestamp") or raw_txn.get("valueDate") or ""
            date_str = str(raw_ts)[:10] if raw_ts else datetime.date.today().isoformat()

            fip_id = raw_txn.get("_fip_id", "HDFC Bank")
            bank_name = fip_id.replace("FIP-", "").replace("-SANDBOX", "").replace("-", " ").title()

            return {
                "date": date_str,
                "description": desc,
                "amount": amount_val,
                "debit": debit_val,
                "credit": credit_val,
                "balance": bal_val,
                "reference_number": ref_no,
                "transaction_type": mode,
                "raw_text": f"Account Aggregator ({bank_name}) - {desc}",
                "source": "account_aggregator",
                "bank": bank_name,
                "account_last4": raw_txn.get("_account_last4", "")
            }
        except Exception as e:
            logger.error(f"[AA Service] Failed normalizing transaction item '{raw_txn}': {e}", exc_info=True)
            return None
