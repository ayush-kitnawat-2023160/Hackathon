"""Judge-facing mandatory API. The UI uses these same endpoints."""

import json
import os
import uuid
from datetime import date

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from app.ingestion import MAX_UPLOAD_BYTES, REQUIRED_TO_SOLVE, load_snapshot, validate_upload
from app.llm import LanguageModel
from app.store import Store
from app.workflow import create_workflow, run_workflow


class Weights(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cost: float = Field(default=0.7, ge=0)
    carbon: float = Field(default=0.3, ge=0)
    lateness: float = Field(default=0, ge=0)


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: str = Field(min_length=2, max_length=3000)
    weights: Weights | None = None
    scenario: dict | None = None


def create_app(db_path=None, llm=None):
    store = Store(db_path or os.getenv("DATABASE_PATH", "data/triplan.sqlite"))
    model = llm or LanguageModel()
    workflow = create_workflow(model)
    app = FastAPI(title="TriPlan Allocation API", version="1.0.0")

    @app.get("/health")
    def health():
        tables, versions = store.snapshot()
        return {"status": "ok", "api": "ready", "database": "ready",
                "llm": model.ready(), "data_ready": REQUIRED_TO_SOLVE <= set(tables),
                "tables_loaded": sorted(tables), "input_versions": versions,
                "planning_start": os.getenv("PLANNING_START", "2026-10-12"),
                "planning_weeks": int(os.getenv("PLANNING_WEEKS", "4"))}

    @app.post("/ingest")
    async def ingest(file: UploadFile = File(...), table: str | None = Form(None),
                     metadata: str | None = Form(None), x_admin_token: str | None = Header(None)):
        expected_token = os.getenv("ADMIN_TOKEN", "")
        if expected_token and x_admin_token != expected_token:
            raise HTTPException(status_code=403, detail="Invalid administrative token")
        try:
            name = table or (json.loads(metadata or "{}").get("table"))
            if isinstance(name, str) and name.strip().startswith("{"):
                name = json.loads(name).get("table")
            content = await file.read(MAX_UPLOAD_BYTES + 1)
            csv_text, count = validate_upload(name, content)
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            return {"rows_loaded": 0, "rows_rejected": 1, "status": "rejected", "error": str(e)}
        checksum = store.put_table(name, csv_text, count)
        tables, _ = store.snapshot()
        return {"rows_loaded": count, "rows_rejected": 0, "status": "loaded",
                "table": name, "checksum": checksum,
                "dataset_ready": REQUIRED_TO_SOLVE <= set(tables)}

    @app.post("/analyze")
    def analyze(body: AnalyzeRequest):
        tables, versions = store.snapshot()
        run_id = "tr-" + uuid.uuid4().hex[:12]
        try:
            planning_start = os.getenv("PLANNING_START", "2026-10-12")
            date.fromisoformat(planning_start)
            data = load_snapshot(tables, versions, start_date=planning_start,
                weeks=int(os.getenv("PLANNING_WEEKS", "4")),
                energy_price_default=float(os.getenv("ENERGY_PRICE_DEFAULT", "8")))
            result, steps = run_workflow(workflow, request=body.request,
                weights=body.weights.model_dump() if body.weights else None,
                scenario=body.scenario, data=data, versions=versions,
                latest=store.latest_plan())
        except (ValueError, KeyError, TypeError) as e:
            # Missing or invalid judge inputs must never crash /analyze.
            result = {"run_id": run_id, "answer_type": "clarification_needed", "summary": str(e),
                      "allocation": [], "kpis": {}, "tradeoff": [], "weights_used": {},
                      "validator": {}, "recommendations": [], "assumptions": [],
                      "limitations": ["Correct the data or request and re-run"],
                      "input_versions": versions, "llm_calls": 0}
            steps = [{"agent": "guard", "tool": "load_dataset_or_validate_request",
                      "output": {"status": "clarification_needed", "reason": str(e)}}]
        store.save_run(result["run_id"], body.request, result, versions, steps)
        return result

    @app.get("/trace/{run_id}")
    def trace(run_id: str):
        result = store.get_trace(run_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return result

    return app


app = create_app()
