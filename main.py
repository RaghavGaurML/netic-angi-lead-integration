import json
import sqlite3
from contextlib import asynccontextmanager
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
                email_status TEXT NOT NULL DEFAULT 'pending'
            )
            """
        )


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


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Netic Angi Lead Integration", lifespan=lifespan)


@app.post("/webhooks/angi", response_model=LeadAccepted)
async def receive_angi_lead(lead: AngiLead, request: Request) -> LeadAccepted:
    tenant_id = TENANT_MAPPING.get(lead.al_account_id)
    if tenant_id is None:
        raise HTTPException(status_code=404, detail="No tenant mapped for ALAccountId")

    raw_payload = await request.json()
    lead_id = insert_lead(lead, tenant_id, raw_payload)
    return LeadAccepted(status="success", lead_id=lead_id, tenant_id=tenant_id)
