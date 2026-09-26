from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional
from abc import ABC, abstractmethod

@dataclass
class TransactionSchema:
    date: str = ""
    description: str = ""
    debit: float = 0.0
    credit: float = 0.0
    amount: float = 0.0
    balance: float = 0.0
    transaction_type: str = ""
    reference_number: str = ""
    raw_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "description": self.description,
            "debit": self.debit,
            "credit": self.credit,
            "amount": self.amount,
            "balance": self.balance,
            "transaction_type": self.transaction_type,
            "reference_number": self.reference_number,
            "raw_text": self.raw_text
        }

class BaseParser(ABC):
    @abstractmethod
    def parse(self, file_path: str) -> List[Dict[str, Any]]:
        """
        Parse the given file and return a list of normalized transaction dictionaries matching TransactionSchema.
        """
        pass
