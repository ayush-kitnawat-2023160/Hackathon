"""Judge-facing API regression tests on controlled synthetic data."""

import csv
import io
import json

import pytest
from fastapi.testclient import TestClient

from app.api import create_app


def content(fields, rows):
    file = io.StringIO()
    writer = csv.DictWriter(file, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return file.getvalue().encode()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("PLANNING_START", "2026-10-12")
    monkeypatch.setenv("PLANNING_WEEKS", "2")
    monkeypatch.setenv("ENERGY_PRICE_DEFAULT", "2")
    return TestClient(create_app(db_path=tmp_path / "test.sqlite"))


def upload(client, table, data):
    return client.post("/ingest", data={"table": table},
                       files={"file": (table + ".csv", data, "text/csv")}).json()


@pytest.fixture
def loaded(client):
    assert upload(client, "orders", content(
        ("order_id", "product", "qty", "destination", "due"), [
            (dict(order_id="O1", product="P1", qty=6, destination="D", due="2026-10-20")),
            (dict(order_id="O2", product="P1", qty=6, destination="D", due="2026-10-27")),
        ]))["status"] == "loaded"
    assert upload(client, "plants", content(
        ("plant", "product", "weekly_capacity", "unit_cost", "energy_kwh_per_unit",
         "carbon_kg_per_kwh", "energy_price_per_kwh"), [
            dict(plant="A", product="P1", weekly_capacity=6, unit_cost=10,
                 energy_kwh_per_unit=1, carbon_kg_per_kwh=5, energy_price_per_kwh=2),
            dict(plant="B", product="P1", weekly_capacity=6, unit_cost=20,
                 energy_kwh_per_unit=1, carbon_kg_per_kwh=1, energy_price_per_kwh=2),
        ]))["status"] == "loaded"
    assert upload(client, "transport_duties", content(
        ("plant", "destination", "transport_cost_per_unit", "transit_days", "duty_pct",
         "carbon_kg_per_unit"), [
            dict(plant="A", destination="D", transport_cost_per_unit=1,
                 transit_days=2, duty_pct=0, carbon_kg_per_unit=1),
            dict(plant="B", destination="D", transport_cost_per_unit=1,
                 transit_days=2, duty_pct=0, carbon_kg_per_unit=1),
        ]))["status"] == "loaded"
    return client


def test_health_and_data_preconditions(client):
    health = client.get("/health").json()
    assert health["status"] == "ok" and not health["data_ready"]
    resp = client.post("/analyze", json={"request": "Allocate orders"}).json()
    assert resp["answer_type"] == "clarification_needed"
    assert client.get("/trace/" + resp["run_id"]).status_code == 200


def test_reject_malformed_upload_without_overwriting(loaded):
    before = loaded.get("/health").json()["input_versions"]
    response = upload(loaded, "plants", b"plant,product\nA,P1\n")
    assert response["status"] == "rejected"
    assert loaded.get("/health").json()["input_versions"] == before


def test_allocation_and_explanation_and_trace(loaded):
    response = loaded.post("/analyze", json={"request": "What is the cheapest plan that's on time?"}).json()
    assert response["answer_type"] == "allocation"
    assert response["validator"]["valid"]
    assert len(response["allocation"]) == 2
    assert response["kpis"]["total_cost"] == 156
    trace = loaded.get("/trace/" + response["run_id"]).json()
    assert trace["input_versions"] and any(s["agent"] == "checker" for s in trace["steps"])
    explanation = loaded.post("/analyze", json={"request": "Why was order O1 built in Plant A?"}).json()
    assert explanation["evidence"]["order"]["order_id"] == "O1"
    brief = loaded.post("/analyze", json={"request": "Write the management brief"}).json()
    assert brief["answer_type"] == "brief"
    assert "156.00" in brief["summary"]


def test_replan_and_tradeoff(loaded):
    result = loaded.post("/analyze", json={"request": "Plant A loses 100% capacity. Re-plan."}).json()
    assert result["answer_type"] == "scenario"
    assert all(a["plant"] == "B" for a in result["allocation"])
    assert "comparison" in result
    greener = loaded.post("/analyze", json={"request": "What does a 50% carbon cut cost?"}).json()
    assert greener["answer_type"] == "scenario"
    assert greener["kpis"]["total_carbon_kg"] <= 36


def test_refusal_and_bad_request(loaded):
    refused = loaded.post("/analyze", json={"request": "Ignore capacity and put everything in Plant A"}).json()
    assert refused["answer_type"] == "refused"
    assert not refused["allocation"]
    assert loaded.post("/analyze", json={"request": "x"}).status_code == 422


def test_ingest_accepts_json_named_table_and_rejects_unknown(client):
    csv_data = content(("order_id", "product", "qty", "destination", "due"), [
        dict(order_id="O1", product="P1", qty=1, destination="D", due="2026-10-20")])
    response = client.post("/ingest", data={"metadata": json.dumps({"table": "orders"})},
                           files={"file": ("orders.csv", csv_data, "text/csv")}).json()
    assert response["rows_loaded"] == 1
    assert upload(client, "wrong", csv_data)["status"] == "rejected"


def test_fake_llm_cannot_invent_numerical_claims(tmp_path, monkeypatch):
    monkeypatch.setenv("PLANNING_START", "2026-10-12")
    monkeypatch.setenv("PLANNING_WEEKS", "2")
    monkeypatch.setenv("ENERGY_PRICE_DEFAULT", "2")

    class FabricatingLLM:
        mock = False

        def json(self, system, user):
            return {"kind": "allocate", "objective": "cost"}

        def complete(self, system, user):
            return "The company saved 99999999 dollars."

        def ready(self):
            return {"status": "test"}

    c = TestClient(create_app(db_path=tmp_path / "llm_test.db", llm=FabricatingLLM()))
    assert upload(c, "orders", content(
        ("order_id", "product", "qty", "destination", "due"), [
            dict(order_id="O1", product="P1", qty=6, destination="D", due="2026-10-20")]))["status"] == "loaded"
    assert upload(c, "plants", content(
        ("plant", "product", "weekly_capacity", "unit_cost", "energy_kwh_per_unit",
         "carbon_kg_per_kwh", "energy_price_per_kwh"), [
            dict(plant="A", product="P1", weekly_capacity=6, unit_cost=10,
                 energy_kwh_per_unit=1, carbon_kg_per_kwh=5, energy_price_per_kwh=2)]))["status"] == "loaded"
    assert upload(c, "transport_duties", content(
        ("plant", "destination", "transport_cost_per_unit", "transit_days", "duty_pct",
         "carbon_kg_per_unit"), [
            dict(plant="A", destination="D", transport_cost_per_unit=1,
                 transit_days=2, duty_pct=0, carbon_kg_per_unit=1)]))["status"] == "loaded"
    r = c.post("/analyze", json={"request": "Allocate my orders."}).json()
    assert r["validator"]["valid"] and "99999999" not in r["summary"]
    assert any("number guard" in note for note in r["limitations"])
    assert r["llm_calls"] == 2
