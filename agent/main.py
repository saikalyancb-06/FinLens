"""
KredoAgent Desktop App Launcher Entry Point
-------------------------------------------
Single executable entry point for KredoAgent.exe.
"""
import sys
import os
import argparse
import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("KredoAgent")

from agent.core.runner import LocalJobRunner
from agent.adapters.mock_adapter import MockBankServer


def main():
    parser = argparse.ArgumentParser(description="KredoAgent Local RPA Runner")
    parser.add_argument("--server", default="http://127.0.0.1:8000", help="Kredo Server URL")
    parser.add_argument("--token", default="", help="User access token")
    parser.add_argument("--job-id", default="", help="Single job ID to process")
    parser.add_argument("--bank", default="mock_bank", help="Bank name")
    parser.add_argument("--username", default="testuser", help="Local username (in-memory only)")
    parser.add_argument("--password", default="testpass", help="Local password (in-memory only)")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")

    args = parser.parse_args()

    logger.info("==================================================")
    logger.info("          KredoAgent.exe Desktop Agent            ")
    logger.info("==================================================")

    # Start local Mock Bank server if testing mock bank
    mock_server = None
    if args.bank == "mock_bank":
        mock_server = MockBankServer(port=8888)
        mock_server.start()

    runner = LocalJobRunner(server_url=args.server, access_token=args.token, headless=args.headless)

    job_metadata = {
        "job_id": args.job_id or "test-agent-job-123",
        "bank_name": args.bank,
        "start_date": "2026-08-01",
        "end_date": "2026-08-31"
    }

    local_credentials = {
        "username": args.username,
        "password": args.password,
    }

    try:
        stmt_id = asyncio.run(runner.execute_job(job_metadata, local_credentials))
        logger.info(f"SUCCESS: Statement imported with ID {stmt_id}")
    finally:
        if mock_server:
            mock_server.stop()


if __name__ == "__main__":
    main()
