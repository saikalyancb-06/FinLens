import time
import logging
from typing import Dict, Any
from collections import Counter

logger = logging.getLogger(__name__)

class MetricsCollector:
    """
    Centralized In-Memory Metrics Collector tracking system processing performance,
    parser accuracy, validation failures, ML latency, API latencies, upload rates, and queue stats.
    """
    def __init__(self):
        self.upload_total = 0
        self.upload_success = 0
        self.upload_failed = 0

        self.parsing_jobs_total = 0
        self.parsing_jobs_success = 0
        self.parsing_jobs_failed = 0

        self.total_transactions_parsed = 0
        self.total_transactions_valid = 0
        self.validation_failures = Counter()

        self.processing_time_seconds = []
        self.ml_inference_time_seconds = []
        self.api_latency_seconds = []

        self.queue_length = 0

    def record_upload(self, success: bool = True):
        self.upload_total += 1
        if success:
            self.upload_success += 1
        else:
            self.upload_failed += 1

    def record_parsing_job(self, duration_sec: float, total_extracted: int, total_valid: int, val_errors: list, success: bool = True):
        self.parsing_jobs_total += 1
        if success:
            self.parsing_jobs_success += 1
        else:
            self.parsing_jobs_failed += 1

        self.processing_time_seconds.append(round(duration_sec, 4))
        # Keep window of last 1000 jobs for memory efficiency
        if len(self.processing_time_seconds) > 1000:
            self.processing_time_seconds.pop(0)

        self.total_transactions_parsed += total_extracted
        self.total_transactions_valid += total_valid

        for err in val_errors:
            err_type = str(err.get("error_type", err.get("type", "Unknown")))
            self.validation_failures[err_type] += 1

    def record_ml_inference(self, duration_sec: float):
        self.ml_inference_time_seconds.append(round(duration_sec, 4))
        if len(self.ml_inference_time_seconds) > 1000:
            self.ml_inference_time_seconds.pop(0)

    def record_api_latency(self, duration_sec: float):
        self.api_latency_seconds.append(round(duration_sec, 4))
        if len(self.api_latency_seconds) > 1000:
            self.api_latency_seconds.pop(0)

    def set_queue_length(self, length: int):
        self.queue_length = max(0, length)

    def get_metrics_summary(self) -> Dict[str, Any]:
        avg_processing_time = (
            sum(self.processing_time_seconds) / len(self.processing_time_seconds)
            if self.processing_time_seconds else 0.0
        )
        avg_ml_inference_time = (
            sum(self.ml_inference_time_seconds) / len(self.ml_inference_time_seconds)
            if self.ml_inference_time_seconds else 0.0
        )
        avg_api_latency = (
            sum(self.api_latency_seconds) / len(self.api_latency_seconds)
            if self.api_latency_seconds else 0.0
        )

        parsing_accuracy = (
            (self.total_transactions_valid / self.total_transactions_parsed * 100.0)
            if self.total_transactions_parsed > 0 else 100.0
        )

        upload_success_rate = (
            (self.upload_success / self.upload_total * 100.0)
            if self.upload_total > 0 else 100.0
        )

        return {
            "processing_time": {
                "avg_seconds": round(avg_processing_time, 4),
                "total_jobs": self.parsing_jobs_total,
                "successful_jobs": self.parsing_jobs_success,
                "failed_jobs": self.parsing_jobs_failed
            },
            "parsing_accuracy": {
                "accuracy_percentage": round(parsing_accuracy, 2),
                "total_extracted_transactions": self.total_transactions_parsed,
                "total_valid_transactions": self.total_transactions_valid
            },
            "validation_failures": dict(self.validation_failures),
            "ml_inference_time": {
                "avg_seconds": round(avg_ml_inference_time, 4),
                "total_inferences": len(self.ml_inference_time_seconds)
            },
            "api_latency": {
                "avg_seconds": round(avg_api_latency, 4),
                "avg_ms": round(avg_api_latency * 1000, 2)
            },
            "upload_stats": {
                "total_uploads": self.upload_total,
                "successful_uploads": self.upload_success,
                "failed_uploads": self.upload_failed,
                "success_rate_percentage": round(upload_success_rate, 2)
            },
            "queue": {
                "current_queue_length": self.queue_length
            }
        }

metrics_collector = MetricsCollector()
