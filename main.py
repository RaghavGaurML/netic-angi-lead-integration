import json
import os
import smtplib
import sqlite3
import ssl
from contextlib import asynccontextmanager
from email.message import EmailMessage
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field


DB_PATH = Path(__file__).with_name("leads.db")

TENANT_MAPPING = {
    "123456": "tenant_001",
}


class PostalAddress(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    address_first_line: str = Field(alias="AddressFirstLine")
    address_second_line: str = Field(alias="AddressSecondLine")
    city: str = Field(alias="City")
    state: str = Field(alias="State")
    postal_code: str = Field(alias="PostalCode")


class AngiLead(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    first_name: str = Field(alias="FirstName")
    last_name: str = Field(alias="LastName")
    phone_number: str = Field(alias="PhoneNumber")
    postal_address: PostalAddress = Field(alias="PostalAddress")
    email: str = Field(alias="Email")
    source: str = Field(alias="Source")
    description: str = Field(alias="Description")
    category: str = Field(alias="Category")
    urgency: str = Field(alias="Urgency")
    correlation_id: str = Field(alias="CorrelationId")
    al_account_id: str = Field(alias="ALAccountId")


class LeadAccepted(BaseModel):
    status: str
    lead_id: int
    tenant_id: str


class AngiAnalytics(BaseModel):
    total_leads: int
    emails_sent: int
    email_failures: int
    email_success_rate: float | None
    average_speed_to_lead_seconds: float | None
    leads_by_category: dict[str, int]
    leads_by_urgency: dict[str, int]


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                phone_number TEXT NOT NULL,
                email TEXT NOT NULL,
                address_first_line TEXT NOT NULL,
                address_second_line TEXT NOT NULL,
                city TEXT NOT NULL,
                state TEXT NOT NULL,
                postal_code TEXT NOT NULL,
                source TEXT NOT NULL,
                description TEXT NOT NULL,
                category TEXT NOT NULL,
                urgency TEXT NOT NULL,
                raw_payload TEXT NOT NULL,
                received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                email_status TEXT NOT NULL DEFAULT 'pending',
                email_sent_at TEXT,
                email_error TEXT
            )
            """
        )

        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(leads)")
        }
        if "email_sent_at" not in columns:
            connection.execute("ALTER TABLE leads ADD COLUMN email_sent_at TEXT")
        if "email_error" not in columns:
            connection.execute("ALTER TABLE leads ADD COLUMN email_error TEXT")


def insert_lead(lead: AngiLead, tenant_id: str, raw_payload: dict) -> int:
    address = lead.postal_address
    with sqlite3.connect(DB_PATH) as connection:
        cursor = connection.execute(
            """
            INSERT INTO leads (
                tenant_id, correlation_id, first_name, last_name,
                phone_number, email, address_first_line,
                address_second_line, city, state, postal_code, source,
                description, category, urgency, raw_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant_id,
                lead.correlation_id,
                lead.first_name,
                lead.last_name,
                lead.phone_number,
                lead.email,
                address.address_first_line,
                address.address_second_line,
                address.city,
                address.state,
                address.postal_code,
                lead.source,
                lead.description,
                lead.category,
                lead.urgency,
                json.dumps(raw_payload),
            ),
        )
        return cursor.lastrowid


def find_lead_by_correlation_id(
    correlation_id: str,
) -> tuple[int, str, str, str] | None:
    with sqlite3.connect(DB_PATH) as connection:
        return connection.execute(
            """
            SELECT id, tenant_id, email_status, raw_payload
            FROM leads
            WHERE correlation_id = ?
            ORDER BY
                CASE email_status
                    WHEN 'sent' THEN 0
                    WHEN 'pending' THEN 1
                    WHEN 'failed' THEN 2
                    ELSE 3
                END,
                id
            LIMIT 1
            """,
            (correlation_id,),
        ).fetchone()


def send_intro_email(lead: AngiLead) -> None:
    required_settings = (
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "EMAIL_FROM",
    )
    settings = {name: os.getenv(name) for name in required_settings}
    missing = [name for name, value in settings.items() if not value]
    if missing:
        raise RuntimeError(
            f"Missing required SMTP configuration: {', '.join(missing)}"
        )

    try:
        smtp_port = int(settings["SMTP_PORT"])
    except ValueError as error:
        raise RuntimeError("SMTP_PORT must be an integer") from error

    message = EmailMessage()
    message["Subject"] = "Thanks for contacting us"
    message["From"] = settings["EMAIL_FROM"]
    message["To"] = lead.email
    message.set_content(
        f"Hi {lead.first_name},\n\n"
        f"Thanks for reaching out about your {lead.category} request. "
        "We received your request and would be happy to help you get an "
        "appointment scheduled.\n\n"
        "Best,\n"
        "The scheduling team"
    )

    with smtplib.SMTP(settings["SMTP_HOST"], smtp_port, timeout=10) as smtp:
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
        smtp.login(settings["SMTP_USERNAME"], settings["SMTP_PASSWORD"])
        refused_recipients = smtp.send_message(message)

    if refused_recipients:
        raise smtplib.SMTPRecipientsRefused(refused_recipients)


def mark_email_sent(lead_id: int) -> None:
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            UPDATE leads
            SET email_status = 'sent',
                email_sent_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                email_error = NULL
            WHERE id = ?
            """,
            (lead_id,),
        )


def mark_email_failed(lead_id: int, error: Exception) -> None:
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            UPDATE leads
            SET email_status = 'failed', email_sent_at = NULL, email_error = ?
            WHERE id = ?
            """,
            (str(error), lead_id),
        )


def mark_email_pending(lead_id: int) -> None:
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            UPDATE leads
            SET email_status = 'pending', email_sent_at = NULL, email_error = NULL
            WHERE id = ?
            """,
            (lead_id,),
        )


def deliver_intro_email(lead_id: int, lead: AngiLead) -> None:
    try:
        send_intro_email(lead)
    except Exception as error:
        mark_email_failed(lead_id, error)
        raise HTTPException(
            status_code=502,
            detail="Lead stored, but introductory email could not be sent",
        ) from error

    mark_email_sent(lead_id)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Netic Angi Lead Integration", lifespan=lifespan)


@app.get("/analytics/angi", response_model=AngiAnalytics)
def get_angi_analytics() -> AngiAnalytics:
    with sqlite3.connect(DB_PATH) as connection:
        total_leads, emails_sent, email_failures, average_speed = (
            connection.execute(
                """
                SELECT
                    COUNT(*),
                    COALESCE(SUM(email_status = 'sent'), 0),
                    COALESCE(SUM(email_status = 'failed'), 0),
                    AVG(
                        CASE
                            WHEN email_status = 'sent' AND email_sent_at IS NOT NULL
                            THEN (julianday(email_sent_at) - julianday(received_at))
                                 * 86400.0
                        END
                    )
                FROM leads
                """
            ).fetchone()
        )
        leads_by_category = dict(
            connection.execute(
                "SELECT category, COUNT(*) FROM leads GROUP BY category"
            ).fetchall()
        )
        leads_by_urgency = dict(
            connection.execute(
                "SELECT urgency, COUNT(*) FROM leads GROUP BY urgency"
            ).fetchall()
        )

    completed_attempts = emails_sent + email_failures
    success_rate = (
        round(emails_sent / completed_attempts, 4)
        if completed_attempts
        else None
    )
    return AngiAnalytics(
        total_leads=total_leads,
        emails_sent=emails_sent,
        email_failures=email_failures,
        email_success_rate=success_rate,
        average_speed_to_lead_seconds=(
            round(average_speed, 4) if average_speed is not None else None
        ),
        leads_by_category=leads_by_category,
        leads_by_urgency=leads_by_urgency,
    )


@app.post("/webhooks/angi", response_model=LeadAccepted)
async def receive_angi_lead(lead: AngiLead, request: Request) -> LeadAccepted:
    tenant_id = TENANT_MAPPING.get(lead.al_account_id)
    if tenant_id is None:
        raise HTTPException(status_code=404, detail="No tenant mapped for ALAccountId")

    raw_payload = await request.json()
    existing_lead = find_lead_by_correlation_id(lead.correlation_id)
    if existing_lead is not None:
        lead_id, stored_tenant_id, email_status, stored_payload = existing_lead

        if email_status in {"sent", "pending"}:
            return LeadAccepted(
                status="success",
                lead_id=lead_id,
                tenant_id=stored_tenant_id,
            )

        if email_status == "failed":
            stored_lead = AngiLead.model_validate(json.loads(stored_payload))
            mark_email_pending(lead_id)
            deliver_intro_email(lead_id, stored_lead)
            return LeadAccepted(
                status="success",
                lead_id=lead_id,
                tenant_id=stored_tenant_id,
            )

        raise HTTPException(status_code=500, detail="Lead has an unknown email status")

    lead_id = insert_lead(lead, tenant_id, raw_payload)
    deliver_intro_email(lead_id, lead)
    return LeadAccepted(status="success", lead_id=lead_id, tenant_id=tenant_id)
