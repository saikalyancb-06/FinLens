import os
import logging
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from financial_parser.engine import OfflineFinancialParserEngine
from financial_parser.services.file_router import FileRouter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FinancialParserAPI")

app = FastAPI(
    title="Production Offline Financial Document Parsing Engine",
    description="Offline document intelligence engine extracting structured ExtractionResult dataset.",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

engine = OfflineFinancialParserEngine()
router = FileRouter()

@app.post("/api/v1/stage1/route")
async def verify_stage1_route(file: UploadFile = File(...)):
    filename = file.filename
    if not filename:
        raise HTTPException(status_code=400, detail="Empty filename")

    try:
        content = await file.read()
        res = router.route(content, filename)
        return JSONResponse(content=res)
    except Exception as e:
        logger.error(f"Stage 1 error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v2/parse")
async def parse_financial_document(file: UploadFile = File(...)):
    filename = file.filename
    if not filename:
        raise HTTPException(status_code=400, detail="Empty filename")

    try:
        content = await file.read()
        result = engine.process_document(content, filename)
        return JSONResponse(content=result.model_dump())
    except Exception as e:
        logger.error(f"Error parsing document {filename}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to parse document: {str(e)}")

from preprocessing import extract_derived_features

@app.post("/api/v2/stage2-normalize")
async def verify_stage2_normalize(file: UploadFile = File(...)):
    filename = file.filename
    if not filename:
        raise HTTPException(status_code=400, detail="Empty filename")

    try:
        content = await file.read()
        result = engine.process_document(content, filename)
        
        normalized_records = []
        total_credit = 0.0
        total_debit = 0.0

        for tx in result.transactions:
            features = extract_derived_features(tx.description, tx.debit, tx.credit)
            if tx.credit > 0:
                total_credit += tx.credit
            if tx.debit > 0:
                total_debit += tx.debit

            normalized_records.append({
                "date": tx.date,
                "description": tx.description,
                "debit": tx.debit,
                "credit": tx.credit,
                "balance": tx.balance,
                "confidence": tx.confidence,
                "ml_features": features
            })

        return JSONResponse(content={
            "document_type": result.document_type,
            "total_transactions": result.total_transactions,
            "summary": {
                "total_credit": total_credit,
                "total_debit": total_debit
            },
            "records": normalized_records
        })
    except Exception as e:
        logger.error(f"Stage 2 Normalization error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

from layer3_classification.classifier import ClassificationEngine
from layer2_normalization.models import Transaction, parse_date_canonical

classifier_engine = ClassificationEngine("configs/rules.yaml")

@app.post("/api/v3/full-pipeline")
async def full_pipeline_parse(file: UploadFile = File(...)):
    filename = file.filename
    if not filename:
        raise HTTPException(status_code=400, detail="Empty filename")

    try:
        content = await file.read()

        # Stage 1 File Routing
        route_info = router.route(content, filename)

        # Stage 2 Extraction & Normalization
        extraction_res = engine.process_document(content, filename)

        # Stage 3 Classification
        classified_txs = []
        for tx in extraction_res.transactions:
            # Parse date string to python date object expected by Layer 2 Transaction model
            parsed_tx_date = parse_date_canonical(tx.date)
            # Map UniversalTransaction to Layer 2 Transaction
            t_obj = Transaction(
                transaction_date=parsed_tx_date,
                narration=tx.description,
                withdrawal=tx.debit if tx.debit > 0 else None,
                deposit=tx.credit if tx.credit > 0 else None,
                balance=tx.balance,
                balance_dr_cr="Cr",
                source_file=filename,
                source_sheet=None,
                account_id="DEFAULT"
            )
            cat_tx = classifier_engine.classify_transaction(t_obj)
            classified_txs.append({
                "date": tx.date,
                "description": tx.description,
                "debit": tx.debit,
                "credit": tx.credit,
                "balance": tx.balance,
                "confidence": tx.confidence,
                "category": cat_tx.category,
                "classification_method": cat_tx.method,
                "classification_confidence": cat_tx.confidence
            })

        return JSONResponse(content={
            "stage1_routing": route_info,
            "stage2_extraction": {
                "document_type": extraction_res.document_type,
                "file_type": extraction_res.file_type,
                "header_confidence": extraction_res.header_confidence,
                "total_transactions": extraction_res.total_transactions,
                "failed_rows": extraction_res.failed_rows,
                "validation_errors": extraction_res.validation_errors
            },
            "stage3_classified_transactions": classified_txs
        })
    except Exception as e:
        logger.error(f"Full pipeline error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
def health():
    return {"status": "healthy", "engine": "Offline Financial Document Intelligence Engine v2.0"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
