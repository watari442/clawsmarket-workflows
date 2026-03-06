#!/usr/bin/env python3
"""MCP server for Reoon — email verification."""

import json
import os
import asyncio

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

API_KEY = os.environ.get("REOON_API_KEY", "")
API_URL = "https://emailverifier.reoon.com/api/v1"

STATUS_MAP = {
    "safe": "safe",
    "valid": "safe",
    "invalid": "invalid",
    "disabled": "invalid",
    "disposable": "invalid",
    "spamtrap": "invalid",
    "inbox_full": "invalid",
    "catch_all": "uncertain",
    "role_account": "invalid",
    "unknown": "uncertain",
}

server = Server("reoon")


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="verify_email",
            description=(
                "Verify if an email address is deliverable using Reoon Power Mode. "
                "Returns 'safe' (send), 'invalid' (do not send), or 'uncertain' (catch-all/unknown). "
                "Takes ~3 seconds per email. Only verify emails you intend to send to."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "email": {
                        "type": "string",
                        "description": "Email address to verify",
                    },
                },
                "required": ["email"],
            },
        ),
        Tool(
            name="verify_emails_batch",
            description=(
                "Verify multiple email addresses. Processes sequentially with 0.5s delay "
                "between calls. Returns results for each email."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "emails": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of email addresses to verify (max 20 per batch)",
                    },
                },
                "required": ["emails"],
            },
        ),
        Tool(
            name="check_balance",
            description="Check remaining Reoon verification credits.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if not API_KEY:
        return [TextContent(type="text", text="Error: REOON_API_KEY not configured")]

    if name == "verify_email":
        return await _verify_single(arguments["email"])
    elif name == "verify_emails_batch":
        return await _verify_batch(arguments.get("emails", []))
    elif name == "check_balance":
        return await _check_balance()
    return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def _verify_single(email: str):
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            r = await client.get(
                f"{API_URL}/verify",
                params={"email": email, "key": API_KEY, "mode": "power"},
            )
            r.raise_for_status()
    except Exception as e:
        return [TextContent(type="text", text=f"Reoon error: {e}")]

    data = r.json()
    raw_status = data.get("status", "unknown")
    mapped = STATUS_MAP.get(raw_status, "uncertain")

    return [TextContent(type="text", text=json.dumps({
        "email": email,
        "status": mapped,
        "raw_status": raw_status,
        "safe_to_send": mapped == "safe",
    }))]


async def _verify_batch(emails: list):
    if len(emails) > 20:
        return [TextContent(type="text", text="Error: max 20 emails per batch")]

    results = []
    async with httpx.AsyncClient(timeout=90.0) as client:
        for email in emails:
            try:
                r = await client.get(
                    f"{API_URL}/verify",
                    params={"email": email, "key": API_KEY, "mode": "power"},
                )
                r.raise_for_status()
                data = r.json()
                raw_status = data.get("status", "unknown")
                mapped = STATUS_MAP.get(raw_status, "uncertain")
                results.append({
                    "email": email,
                    "status": mapped,
                    "raw_status": raw_status,
                    "safe_to_send": mapped == "safe",
                })
            except Exception as e:
                results.append({"email": email, "status": "error", "error": str(e)})
            await asyncio.sleep(0.5)

    safe = sum(1 for r in results if r.get("safe_to_send"))
    return [TextContent(type="text", text=json.dumps({
        "total": len(results),
        "safe": safe,
        "invalid": sum(1 for r in results if r["status"] == "invalid"),
        "uncertain": sum(1 for r in results if r["status"] == "uncertain"),
        "results": results,
    }, indent=2))]


async def _check_balance():
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{API_URL}/check-account-balance/",
                params={"key": API_KEY},
            )
            r.raise_for_status()
    except Exception as e:
        return [TextContent(type="text", text=f"Reoon balance check error: {e}")]

    data = r.json()
    return [TextContent(type="text", text=json.dumps({
        "daily_credits_remaining": data.get("remaining_daily_credits"),
        "instant_credits_remaining": data.get("remaining_instant_credits"),
    }))]


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
