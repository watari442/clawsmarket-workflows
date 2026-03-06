#!/usr/bin/env python3
"""MCP server for ClawsMarket pipeline — persists leads & emails to Supabase.

Every SDR deployment gets this MCP server injected automatically.
The NanoBot agent calls these tools to push data from Apollo/Reoon/Instantly
into Supabase, where the dashboard reads it.

Env vars (injected by provisioner):
  SUPABASE_URL        — e.g. https://xyz.supabase.co
  SUPABASE_KEY        — service-role key
  DEPLOYMENT_ID       — this deployment's UUID
"""

import json
import os
import asyncio
import uuid
from datetime import datetime, timezone

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
DEPLOYMENT_ID = os.environ.get("DEPLOYMENT_ID", "")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

server = Server("pipeline")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _id():
    return uuid.uuid4().hex[:24]


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="save_leads",
            description=(
                "Save or update leads in the ClawsMarket dashboard. Upserts by email — "
                "if a lead with the same email exists, it updates; otherwise it creates a new one. "
                "Call this after searching, scoring, or enriching leads so the dashboard stays current."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "leads": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "email": {"type": "string", "description": "Lead's email address"},
                                "name": {"type": "string", "description": "Full name"},
                                "company": {"type": "string", "description": "Company name"},
                                "title": {"type": "string", "description": "Job title"},
                                "source": {
                                    "type": "string",
                                    "description": "Where the lead came from (e.g. 'apollo')",
                                    "default": "apollo",
                                },
                                "stage": {
                                    "type": "string",
                                    "description": "Pipeline stage: new, enriched, verified, emailed, replied, meeting, disqualified",
                                    "default": "new",
                                },
                                "score": {
                                    "type": "number",
                                    "description": "ICP fit score 0-100",
                                    "default": 0,
                                },
                                "metadata": {
                                    "type": "object",
                                    "description": "Extra data: apollo_id, linkedin, city, state, industry, employees, verification_status, etc.",
                                },
                            },
                            "required": ["email"],
                        },
                        "description": "Array of leads to save (max 50 per call)",
                    },
                },
                "required": ["leads"],
            },
        ),
        Tool(
            name="update_lead",
            description=(
                "Update a single lead's stage, score, or metadata. Use this when a lead progresses "
                "through the pipeline (e.g. new → enriched → verified → emailed → replied → meeting). "
                "Looks up by email first, then by apollo_id in metadata — so it works even when "
                "enrichment changes a placeholder email to the real one."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "email": {"type": "string", "description": "Lead's email (new or existing — used to find AND update)"},
                    "apollo_id": {"type": "string", "description": "Apollo person ID (fallback lookup if email doesn't match)"},
                    "stage": {"type": "string", "description": "New pipeline stage"},
                    "score": {"type": "number", "description": "Updated ICP score"},
                    "metadata": {"type": "object", "description": "Merge into existing metadata"},
                    "name": {"type": "string"},
                    "company": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": [],
            },
        ),
        Tool(
            name="save_emails",
            description=(
                "Record outreach emails in the dashboard. Call this when you draft or send emails "
                "so the dashboard shows the email funnel (sent → opened → replied → bounced)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "emails": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "lead_email": {
                                    "type": "string",
                                    "description": "Email address of the lead this email was sent to",
                                },
                                "subject": {"type": "string", "description": "Email subject line"},
                                "variant": {"type": "string", "description": "A/B variant name if applicable"},
                                "step": {
                                    "type": "integer",
                                    "description": "Sequence step number (1 = initial, 2 = follow-up, etc.)",
                                    "default": 1,
                                },
                                "status": {
                                    "type": "string",
                                    "description": "Email status: draft, sent, opened, replied, bounced",
                                    "default": "draft",
                                },
                                "sent_at": {"type": "string", "description": "ISO timestamp when sent"},
                                "campaign_id": {
                                    "type": "string",
                                    "description": "Instantly campaign ID for tracking",
                                },
                            },
                            "required": ["lead_email"],
                        },
                        "description": "Array of emails to record",
                    },
                },
                "required": ["emails"],
            },
        ),
        Tool(
            name="update_email",
            description=(
                "Update an email's status (e.g. sent → opened, opened → replied, sent → bounced). "
                "Use lead_email + step to find the right email record."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "lead_email": {"type": "string", "description": "Lead's email address"},
                    "step": {"type": "integer", "description": "Sequence step number", "default": 1},
                    "status": {"type": "string", "description": "New status: sent, opened, replied, bounced"},
                    "sent_at": {"type": "string", "description": "ISO timestamp when sent"},
                    "opened_at": {"type": "string", "description": "ISO timestamp when opened"},
                    "replied_at": {"type": "string", "description": "ISO timestamp when replied"},
                    "bounced_at": {"type": "string", "description": "ISO timestamp when bounced"},
                },
                "required": ["lead_email"],
            },
        ),
        Tool(
            name="get_pipeline_stats",
            description=(
                "Get pipeline stats and the console dashboard URL. "
                "IMPORTANT: When the user asks to see the dashboard, stats, or pipeline, "
                "always share the console_url from the response so they can view the interactive dashboard. "
                "Do NOT format stats as a text table — link the user to the dashboard instead."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return [TextContent(type="text", text="Error: SUPABASE_URL/SUPABASE_KEY not configured")]
    if not DEPLOYMENT_ID:
        return [TextContent(type="text", text="Error: DEPLOYMENT_ID not configured")]

    handlers = {
        "save_leads": _save_leads,
        "update_lead": _update_lead,
        "save_emails": _save_emails,
        "update_email": _update_email,
        "get_pipeline_stats": _get_stats,
    }
    handler = handlers.get(name)
    if not handler:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    return await handler(arguments)


async def _find_existing_lead(client: httpx.AsyncClient, email: str, metadata: dict) -> list:
    """Find an existing lead by email OR by apollo_id in metadata.

    This prevents duplicates when enrichment reveals a different email
    than the placeholder stored during initial search.
    """
    # 1. Try exact email match first
    try:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/sdr_leads",
            headers=HEADERS,
            params={
                "deployment_id": f"eq.{DEPLOYMENT_ID}",
                "email": f"eq.{email}",
                "select": "id,email,metadata",
            },
        )
        rows = r.json() if r.status_code == 200 else []
        if rows:
            return rows
    except Exception:
        pass

    # 2. If no email match and we have an apollo_id, look up by that
    apollo_id = metadata.get("apollo_id", "") if metadata else ""
    if apollo_id:
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/sdr_leads",
                headers=HEADERS,
                params={
                    "deployment_id": f"eq.{DEPLOYMENT_ID}",
                    "metadata->>apollo_id": f"eq.{apollo_id}",
                    "select": "id,email,metadata",
                },
            )
            rows = r.json() if r.status_code == 200 else []
            if rows:
                return rows
        except Exception:
            pass

    return []


async def _save_leads(args: dict):
    leads = args.get("leads", [])
    if len(leads) > 50:
        return [TextContent(type="text", text="Error: max 50 leads per call")]

    created, updated, errors = 0, 0, 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        for lead in leads:
            email = lead.get("email")
            if not email:
                errors += 1
                continue

            now = _now()
            metadata = lead.get("metadata", {})
            existing = await _find_existing_lead(client, email, metadata)

            if existing:
                # Update — also update the email if it changed (e.g. placeholder → real)
                patch = {"updated_at": now, "email": email}
                for key in ("name", "company", "title", "source", "stage", "score"):
                    if lead.get(key) is not None:
                        patch[key] = lead[key]
                if metadata:
                    # Merge new metadata into existing
                    old_meta = existing[0].get("metadata") or {}
                    patch["metadata"] = {**old_meta, **metadata}

                try:
                    await client.patch(
                        f"{SUPABASE_URL}/rest/v1/sdr_leads",
                        headers=HEADERS,
                        params={"id": f"eq.{existing[0]['id']}"},
                        json=patch,
                    )
                    updated += 1
                except Exception:
                    errors += 1
            else:
                # Insert
                row = {
                    "id": _id(),
                    "deployment_id": DEPLOYMENT_ID,
                    "email": email,
                    "name": lead.get("name"),
                    "company": lead.get("company"),
                    "title": lead.get("title"),
                    "source": lead.get("source", "apollo"),
                    "stage": lead.get("stage", "new"),
                    "score": lead.get("score", 0),
                    "metadata": metadata,
                    "created_at": now,
                    "updated_at": now,
                }
                try:
                    await client.post(
                        f"{SUPABASE_URL}/rest/v1/sdr_leads",
                        headers=HEADERS,
                        json=row,
                    )
                    created += 1
                except Exception:
                    errors += 1

    return [TextContent(type="text", text=json.dumps({
        "created": created, "updated": updated, "errors": errors,
    }))]


async def _update_lead(args: dict):
    email = args.get("email")
    apollo_id = args.get("apollo_id", "")
    if not email and not apollo_id:
        return [TextContent(type="text", text="Error: email or apollo_id required")]

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Find lead by email first, then by apollo_id
        existing = await _find_existing_lead(client, email or "", args.get("metadata") or {"apollo_id": apollo_id})
        if not existing:
            return [TextContent(type="text", text=f"Lead not found: {email or apollo_id}")]

        lead_id = existing[0]["id"]
        old_meta = existing[0].get("metadata") or {}

        patch = {"updated_at": _now()}
        for key in ("stage", "score", "name", "company", "title"):
            if args.get(key) is not None:
                patch[key] = args[key]

        # Update email if provided and different from stored
        if email and email != existing[0].get("email"):
            patch["email"] = email

        # Merge metadata
        if args.get("metadata"):
            merged = {**old_meta, **args["metadata"]}
            patch["metadata"] = merged

        await client.patch(
            f"{SUPABASE_URL}/rest/v1/sdr_leads",
            headers=HEADERS,
            params={"id": f"eq.{lead_id}"},
            json=patch,
        )

    return [TextContent(type="text", text=json.dumps({"updated": True, "email": email or existing[0].get("email")}))]


async def _save_emails(args: dict):
    items = args.get("emails", [])
    created, errors = 0, 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        for email_data in items:
            lead_email = email_data.get("lead_email")
            if not lead_email:
                errors += 1
                continue

            # Resolve lead_id from email
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/sdr_leads",
                headers=HEADERS,
                params={
                    "deployment_id": f"eq.{DEPLOYMENT_ID}",
                    "email": f"eq.{lead_email}",
                    "select": "id",
                },
            )
            leads = r.json() if r.status_code == 200 else []
            if not leads:
                errors += 1
                continue

            lead_id = leads[0]["id"]
            now = _now()

            row = {
                "id": _id(),
                "deployment_id": DEPLOYMENT_ID,
                "lead_id": lead_id,
                "subject": email_data.get("subject"),
                "variant": email_data.get("variant"),
                "step": email_data.get("step", 1),
                "status": email_data.get("status", "draft"),
                "sent_at": email_data.get("sent_at"),
                "created_at": now,
            }

            try:
                await client.post(
                    f"{SUPABASE_URL}/rest/v1/sdr_emails",
                    headers=HEADERS,
                    json=row,
                )
                created += 1
            except Exception:
                errors += 1

    return [TextContent(type="text", text=json.dumps({"created": created, "errors": errors}))]


async def _update_email(args: dict):
    lead_email = args.get("lead_email")
    if not lead_email:
        return [TextContent(type="text", text="Error: lead_email required")]

    step = args.get("step", 1)

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Find lead
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/sdr_leads",
            headers=HEADERS,
            params={
                "deployment_id": f"eq.{DEPLOYMENT_ID}",
                "email": f"eq.{lead_email}",
                "select": "id",
            },
        )
        leads = r.json() if r.status_code == 200 else []
        if not leads:
            return [TextContent(type="text", text=f"Lead not found: {lead_email}")]

        lead_id = leads[0]["id"]

        # Find email record
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/sdr_emails",
            headers=HEADERS,
            params={
                "deployment_id": f"eq.{DEPLOYMENT_ID}",
                "lead_id": f"eq.{lead_id}",
                "step": f"eq.{step}",
                "select": "id",
            },
        )
        emails = r.json() if r.status_code == 200 else []
        if not emails:
            return [TextContent(type="text", text=f"Email record not found for {lead_email} step {step}")]

        email_id = emails[0]["id"]

        patch = {}
        for key in ("status", "sent_at", "opened_at", "replied_at", "bounced_at"):
            if args.get(key) is not None:
                patch[key] = args[key]

        if not patch:
            return [TextContent(type="text", text="No fields to update")]

        await client.patch(
            f"{SUPABASE_URL}/rest/v1/sdr_emails",
            headers=HEADERS,
            params={"id": f"eq.{email_id}"},
            json=patch,
        )

    return [TextContent(type="text", text=json.dumps({"updated": True, "lead_email": lead_email, "step": step}))]


async def _get_stats(args: dict):
    async with httpx.AsyncClient(timeout=15.0) as client:
        # Get lead counts by stage
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/sdr_leads",
            headers=HEADERS,
            params={
                "deployment_id": f"eq.{DEPLOYMENT_ID}",
                "select": "stage",
            },
        )
        leads = r.json() if r.status_code == 200 else []

        by_stage = {}
        for lead in leads:
            stage = lead.get("stage", "new")
            by_stage[stage] = by_stage.get(stage, 0) + 1

        # Get email counts
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/sdr_emails",
            headers=HEADERS,
            params={
                "deployment_id": f"eq.{DEPLOYMENT_ID}",
                "select": "status",
            },
        )
        emails = r.json() if r.status_code == 200 else []

        email_stats = {"draft": 0, "sent": 0, "opened": 0, "replied": 0, "bounced": 0}
        for e in emails:
            status = e.get("status", "draft")
            email_stats[status] = email_stats.get(status, 0) + 1

    console_url = f"https://www.clawsmarket.com/console/{DEPLOYMENT_ID}"

    return [TextContent(type="text", text=json.dumps({
        "total_leads": len(leads),
        "leads_by_stage": by_stage,
        "total_emails": len(emails),
        "email_funnel": email_stats,
        "console_url": console_url,
        "instruction": "IMPORTANT: Share the console_url with the user so they can see the interactive dashboard. Do NOT format these stats as a text table - tell the user to visit the dashboard URL instead.",
    }, indent=2))]


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
