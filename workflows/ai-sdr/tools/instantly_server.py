#!/usr/bin/env python3
"""MCP server for Instantly.ai — email campaign management."""

import json
import os
import asyncio

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

API_KEY = os.environ.get("INSTANTLY_API_KEY", "")
API_URL = "https://api.instantly.ai/api/v2"
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

server = Server("instantly")


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="create_campaign",
            description=(
                "Create a new email campaign in Instantly with a sending schedule and email sequence. "
                "Returns the campaign ID. The campaign starts in DRAFT — you must activate it separately."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Campaign name"},
                    "sending_accounts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Email accounts to send from, e.g. ['outreach@company.com']",
                    },
                    "subject": {"type": "string", "description": "Email subject line"},
                    "body": {
                        "type": "string",
                        "description": (
                            "Email body (HTML). Use merge variables: "
                            "{{first_name}}, {{company_name}}, {{icebreaker}}, {{landing_url}}"
                        ),
                    },
                    "timezone": {
                        "type": "string",
                        "description": "Timezone for schedule. Default: America/Chicago",
                        "default": "America/Chicago",
                    },
                    "start_hour": {
                        "type": "string",
                        "description": "Start time HH:MM. Default: 09:00",
                        "default": "09:00",
                    },
                    "end_hour": {
                        "type": "string",
                        "description": "End time HH:MM. Default: 17:00",
                        "default": "17:00",
                    },
                },
                "required": ["name", "sending_accounts", "subject", "body"],
            },
        ),
        Tool(
            name="add_leads_to_campaign",
            description=(
                "Add leads to an existing Instantly campaign. Each lead gets custom merge variables "
                "for personalization. Add leads BEFORE activating the campaign."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "campaign_id": {"type": "string", "description": "Instantly campaign ID"},
                    "leads": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "email": {"type": "string"},
                                "first_name": {"type": "string"},
                                "last_name": {"type": "string"},
                                "company_name": {"type": "string"},
                                "icebreaker": {"type": "string"},
                                "landing_url": {"type": "string"},
                                "title": {"type": "string"},
                            },
                            "required": ["email", "first_name"],
                        },
                        "description": "Array of leads to add (max 50 per call)",
                    },
                },
                "required": ["campaign_id", "leads"],
            },
        ),
        Tool(
            name="activate_campaign",
            description=(
                "Activate a campaign to start sending emails. "
                "WARNING: This is irreversible — emails will begin sending immediately. "
                "Make sure leads are loaded and the sequence is correct before activating."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "campaign_id": {"type": "string", "description": "Instantly campaign ID"},
                },
                "required": ["campaign_id"],
            },
        ),
        Tool(
            name="pause_campaign",
            description="Pause a running campaign. Emails already queued may still send.",
            inputSchema={
                "type": "object",
                "properties": {
                    "campaign_id": {"type": "string", "description": "Instantly campaign ID"},
                },
                "required": ["campaign_id"],
            },
        ),
        Tool(
            name="get_campaign_analytics",
            description="Get send/open/reply/bounce stats for a campaign.",
            inputSchema={
                "type": "object",
                "properties": {
                    "campaign_id": {"type": "string", "description": "Instantly campaign ID"},
                },
                "required": ["campaign_id"],
            },
        ),
        Tool(
            name="get_lead_statuses",
            description="Get the current status of all leads in a campaign (opens, replies, bounces).",
            inputSchema={
                "type": "object",
                "properties": {
                    "campaign_id": {"type": "string", "description": "Instantly campaign ID"},
                    "limit": {
                        "type": "integer",
                        "description": "Max leads to return. Default: 100",
                        "default": 100,
                    },
                },
                "required": ["campaign_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if not API_KEY:
        return [TextContent(type="text", text="Error: INSTANTLY_API_KEY not configured")]

    handlers = {
        "create_campaign": _create_campaign,
        "add_leads_to_campaign": _add_leads,
        "activate_campaign": _activate,
        "pause_campaign": _pause,
        "get_campaign_analytics": _analytics,
        "get_lead_statuses": _lead_statuses,
    }
    handler = handlers.get(name)
    if not handler:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    return await handler(arguments)


async def _create_campaign(args: dict):
    tz = args.get("timezone", "America/Chicago")
    body = {
        "name": args["name"],
        "campaign_schedule": {
            "schedules": [{
                "name": "Business Hours",
                "timing": {
                    "from": args.get("start_hour", "09:00"),
                    "to": args.get("end_hour", "17:00"),
                },
                "days": {"0": True, "1": True, "2": True, "3": True, "4": True, "5": False, "6": False},
                "timezone": tz,
            }]
        },
        "email_list": args["sending_accounts"],
        "sequences": [{
            "steps": [{
                "type": "email",
                "delay": 0,
                "variants": [{"subject": args["subject"], "body": args["body"]}],
            }]
        }],
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{API_URL}/campaigns", headers=HEADERS, json=body)
            r.raise_for_status()
    except Exception as e:
        return [TextContent(type="text", text=f"Instantly create_campaign error: {e}")]

    data = r.json()
    return [TextContent(type="text", text=json.dumps({
        "campaign_id": data.get("id"),
        "name": args["name"],
        "status": "draft",
    }))]


async def _add_leads(args: dict):
    campaign_id = args["campaign_id"]
    leads = args.get("leads", [])
    if len(leads) > 50:
        return [TextContent(type="text", text="Error: max 50 leads per call")]

    added, skipped, errors = 0, 0, 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        for lead in leads:
            payload = {
                "email": lead["email"],
                "first_name": lead.get("first_name", ""),
                "last_name": lead.get("last_name", ""),
                "company_name": lead.get("company_name", ""),
                "campaign": campaign_id,
                "custom_variables": {},
            }
            for key in ("icebreaker", "landing_url", "title"):
                if lead.get(key):
                    payload["custom_variables"][key] = lead[key]

            try:
                r = await client.post(f"{API_URL}/leads", headers=HEADERS, json=payload)
                if r.status_code >= 400:
                    text = r.text.lower()
                    if "already exists" in text or "duplicate" in text:
                        skipped += 1
                    else:
                        errors += 1
                else:
                    added += 1
            except Exception:
                errors += 1
            await asyncio.sleep(0.2)

    return [TextContent(type="text", text=json.dumps({
        "campaign_id": campaign_id,
        "added": added,
        "skipped_duplicates": skipped,
        "errors": errors,
    }))]


async def _activate(args: dict):
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{API_URL}/campaigns/{args['campaign_id']}/activate",
                headers=HEADERS, json={},
            )
            r.raise_for_status()
    except Exception as e:
        return [TextContent(type="text", text=f"Activate error: {e}")]
    return [TextContent(type="text", text=json.dumps({"campaign_id": args["campaign_id"], "status": "active"}))]


async def _pause(args: dict):
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{API_URL}/campaigns/{args['campaign_id']}/pause",
                headers=HEADERS, json={},
            )
            r.raise_for_status()
    except Exception as e:
        return [TextContent(type="text", text=f"Pause error: {e}")]
    return [TextContent(type="text", text=json.dumps({"campaign_id": args["campaign_id"], "status": "paused"}))]


async def _analytics(args: dict):
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(
                f"{API_URL}/campaigns/{args['campaign_id']}/analytics",
                headers=HEADERS,
            )
            r.raise_for_status()
    except Exception as e:
        return [TextContent(type="text", text=f"Analytics error: {e}")]

    data = r.json()
    return [TextContent(type="text", text=json.dumps({
        "campaign_id": args["campaign_id"],
        "emails_sent": data.get("emails_sent", 0),
        "opened": data.get("opened", 0),
        "replied": data.get("replied", 0),
        "bounced": data.get("bounced", 0),
    }, indent=2))]


async def _lead_statuses(args: dict):
    campaign_id = args["campaign_id"]
    limit = min(args.get("limit", 100), 100)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(
                f"{API_URL}/leads",
                headers=HEADERS,
                params={"campaign_id": campaign_id, "skip": 0, "limit": limit},
            )
            r.raise_for_status()
    except Exception as e:
        return [TextContent(type="text", text=f"Lead status error: {e}")]

    data = r.json()
    items = data.get("items", data) if isinstance(data, dict) else data

    leads = []
    for item in items[:limit]:
        leads.append({
            "email": item.get("email"),
            "status": item.get("status"),
            "opened": item.get("open_count", 0) > 0,
            "replied": bool(item.get("reply")),
            "bounced": item.get("is_bounced", False),
        })

    return [TextContent(type="text", text=json.dumps({
        "campaign_id": campaign_id,
        "count": len(leads),
        "leads": leads,
    }, indent=2))]


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
