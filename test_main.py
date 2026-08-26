import json
import smtplib
import sqlite3
from unittest.mock import patch

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


def configure_smtp_environment(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USERNAME", "sender@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "test-app-password")
    monkeypatch.setenv("EMAIL_FROM", "sender@example.com")


def test_repeated_sent_lead_reuses_row_and_does_not_resend(tmp_path, monkeypatch):
    database_path = tmp_path / "test_leads.db"
    monkeypatch.setattr(main, "DB_PATH", database_path)
    configure_smtp_environment(monkeypatch)

    with patch("main.smtplib.SMTP") as smtp_class:
        smtp = smtp_class.return_value.__enter__.return_value

        def accept_message(_message):
            with sqlite3.connect(database_path) as connection:
                status_during_send = connection.execute(
                    "SELECT email_status FROM leads WHERE id = 1"
                ).fetchone()
            assert status_during_send == ("pending",)
            return {}

        smtp.send_message.side_effect = accept_message

        with TestClient(main.app) as client:
            first_response = client.post("/webhooks/angi", json=SAMPLE_LEAD)
            retry_response = client.post("/webhooks/angi", json=SAMPLE_LEAD)

    expected_response = {
        "status": "success",
        "lead_id": 1,
        "tenant_id": "tenant_001",
    }
    assert first_response.status_code == 200
    assert first_response.json() == expected_response
    assert retry_response.status_code == 200
    assert retry_response.json() == expected_response

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT tenant_id, correlation_id, first_name, email, raw_payload, "
            "email_status, email_sent_at, email_error "
            "FROM leads WHERE id = 1"
        ).fetchone()
        row_count = connection.execute("SELECT COUNT(*) FROM leads").fetchone()[0]

    assert row[:4] == (
        "tenant_001",
        SAMPLE_LEAD["CorrelationId"],
        "Bob",
        "bob.builder@gmail.com",
    )
    assert json.loads(row[4]) == SAMPLE_LEAD
    assert row[5] == "sent"
    assert row[6] is not None
    assert row[7] is None
    assert row_count == 1

    smtp_class.assert_called_once_with("smtp.example.com", 587, timeout=10)
    smtp.starttls.assert_called_once()
    smtp.login.assert_called_once_with("sender@example.com", "test-app-password")
    sent_message = smtp.send_message.call_args.args[0]
    assert sent_message["To"] == SAMPLE_LEAD["Email"]
    assert "Hi Bob" in sent_message.get_content()


def test_retry_after_email_failure_reuses_row_and_sends_again(tmp_path, monkeypatch):
    database_path = tmp_path / "test_leads.db"
    monkeypatch.setattr(main, "DB_PATH", database_path)
    configure_smtp_environment(monkeypatch)

    refusal = smtplib.SMTPRecipientsRefused(
        {SAMPLE_LEAD["Email"]: (550, b"recipient rejected")}
    )
    with patch("main.smtplib.SMTP") as smtp_class:
        smtp = smtp_class.return_value.__enter__.return_value
        attempts = 0

        def reject_then_accept(_message):
            nonlocal attempts
            attempts += 1
            with sqlite3.connect(database_path) as connection:
                status_during_send = connection.execute(
                    "SELECT email_status FROM leads WHERE id = 1"
                ).fetchone()
            assert status_during_send == ("pending",)
            if attempts == 1:
                raise refusal
            return {}

        smtp.send_message.side_effect = reject_then_accept

        with TestClient(main.app) as client:
            first_response = client.post("/webhooks/angi", json=SAMPLE_LEAD)
            retry_response = client.post("/webhooks/angi", json=SAMPLE_LEAD)

    assert first_response.status_code == 502
    assert first_response.json() == {
        "detail": "Lead stored, but introductory email could not be sent"
    }
    assert retry_response.status_code == 200
    assert retry_response.json() == {
        "status": "success",
        "lead_id": 1,
        "tenant_id": "tenant_001",
    }

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT email, email_status, email_sent_at, email_error "
            "FROM leads WHERE id = 1"
        ).fetchone()
        row_count = connection.execute("SELECT COUNT(*) FROM leads").fetchone()[0]

    assert row[0] == SAMPLE_LEAD["Email"]
    assert row[1] == "sent"
    assert row[2] is not None
    assert row[3] is None
    assert row_count == 1
    assert smtp.send_message.call_count == 2


def test_pending_lead_is_reused_without_sending(tmp_path, monkeypatch):
    database_path = tmp_path / "test_leads.db"
    monkeypatch.setattr(main, "DB_PATH", database_path)
    main.init_db()
    lead = main.AngiLead.model_validate(SAMPLE_LEAD)
    existing_lead_id = main.insert_lead(lead, "tenant_001", SAMPLE_LEAD)

    with patch("main.smtplib.SMTP") as smtp_class:
        with TestClient(main.app) as client:
            response = client.post("/webhooks/angi", json=SAMPLE_LEAD)

    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "lead_id": existing_lead_id,
        "tenant_id": "tenant_001",
    }

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT id, email_status FROM leads"
        ).fetchall()

    assert rows == [(existing_lead_id, "pending")]
    smtp_class.assert_not_called()
