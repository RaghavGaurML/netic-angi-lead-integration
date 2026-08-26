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


def test_successful_email_marks_persisted_lead_sent(tmp_path, monkeypatch):
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
            response = client.post("/webhooks/angi", json=SAMPLE_LEAD)

    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "lead_id": 1,
        "tenant_id": "tenant_001",
    }

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT tenant_id, correlation_id, first_name, email, raw_payload, "
            "email_status, email_sent_at, email_error "
            "FROM leads WHERE id = 1"
        ).fetchone()

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

    smtp_class.assert_called_once_with("smtp.example.com", 587, timeout=10)
    smtp.starttls.assert_called_once()
    smtp.login.assert_called_once_with("sender@example.com", "test-app-password")
    sent_message = smtp.send_message.call_args.args[0]
    assert sent_message["To"] == SAMPLE_LEAD["Email"]
    assert "Hi Bob" in sent_message.get_content()


def test_email_failure_keeps_lead_and_marks_it_failed(tmp_path, monkeypatch):
    database_path = tmp_path / "test_leads.db"
    monkeypatch.setattr(main, "DB_PATH", database_path)
    configure_smtp_environment(monkeypatch)

    refusal = smtplib.SMTPRecipientsRefused(
        {SAMPLE_LEAD["Email"]: (550, b"recipient rejected")}
    )
    with patch("main.smtplib.SMTP") as smtp_class:
        smtp = smtp_class.return_value.__enter__.return_value

        def reject_message(_message):
            with sqlite3.connect(database_path) as connection:
                status_during_send = connection.execute(
                    "SELECT email_status FROM leads WHERE id = 1"
                ).fetchone()
            assert status_during_send == ("pending",)
            raise refusal

        smtp.send_message.side_effect = reject_message

        with TestClient(main.app) as client:
            response = client.post("/webhooks/angi", json=SAMPLE_LEAD)

    assert response.status_code == 502
    assert response.json() == {
        "detail": "Lead stored, but introductory email could not be sent"
    }

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT email, email_status, email_sent_at, email_error "
            "FROM leads WHERE id = 1"
        ).fetchone()

    assert row[0] == SAMPLE_LEAD["Email"]
    assert row[1] == "failed"
    assert row[2] is None
    assert "recipient rejected" in row[3]
