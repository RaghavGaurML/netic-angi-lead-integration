import json
import sqlite3

from fastapi.testclient import TestClient

import main


SAMPLE_LEAD = {
    "FirstName": "Bob",
    "LastName": "Builder",
    "PhoneNumber": "5554332646",
    "PostalAddress": {
        "AddressFirstLine": "123 Main St.",
        "AddressSecondLine": "",
        "City": "Indianapolis",
        "State": "IN",
        "PostalCode": "46203",
    },
    "Email": "bob.builder@gmail.com",
    "Source": "Angie's List Quote Request",
    "Description": "I'm looking for recurring house cleaning services please.",
    "Category": "Indianapolis - House Cleaning",
    "Urgency": "This Week",
    "CorrelationId": "61a7de56-dba3-4e59-8e2a-3fa827f84f7f",
    "ALAccountId": "123456",
}


def test_valid_lead_is_mapped_and_persisted(tmp_path, monkeypatch):
    database_path = tmp_path / "test_leads.db"
    monkeypatch.setattr(main, "DB_PATH", database_path)

    with TestClient(main.app) as client:
        response = client.post("/webhooks/angi", json=SAMPLE_LEAD)

    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "lead_id": 1,
        "tenant_id": "tenant_001",
    }

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT tenant_id, correlation_id, first_name, email, raw_payload "
            "FROM leads WHERE id = 1"
        ).fetchone()

    assert row[:4] == (
        "tenant_001",
        SAMPLE_LEAD["CorrelationId"],
        "Bob",
        "bob.builder@gmail.com",
    )
    assert json.loads(row[4]) == SAMPLE_LEAD
