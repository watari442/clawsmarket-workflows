#!/usr/bin/env python3
"""MCP server for ClawsMarket dashboard sync — issues, fixes, PRs.

Env vars:
  SUPABASE_URL     — Supabase REST URL
  SUPABASE_KEY     — Service role key
  DEPLOYMENT_ID    — This deployment's UUID
"""

import asyncio
import json
import os

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


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="sync_issues",
            description="Sync Sentry issues to the ClawsMarket dashboard. Call after polling issues.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issues": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "sentry_issue_id": {"type": "string"},
                                "sentry_short_id": {"type": "string"},
                                "title": {"type": "string"},
                                "level": {"type": "string"},
                                "event_count": {"type": "integer"},
                                "status": {"type": "string"},
                                "sentry_url": {"type": "string"},
                            },
                            "required": ["sentry_issue_id", "title"],
                        },
                    },
                },
                "required": ["issues"],
            },
        ),
        Tool(
            name="sync_fixes",
            description="Sync a fix to the dashboard. Call after generating a fix.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sentry_issue_id": {"type": "string"},
                    "files_changed": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "string"},
                    "explanation": {"type": "string"},
                },
                "required": ["sentry_issue_id"],
            },
        ),
        Tool(
            name="sync_prs",
            description="Sync a PR to the dashboard. Call after creating a PR.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sentry_issue_id": {"type": "string"},
                    "pr_number": {"type": "integer"},
                    "pr_url": {"type": "string"},
                    "branch": {"type": "string"},
                },
                "required": ["sentry_issue_id", "pr_url"],
            },
        ),
        Tool(
            name="get_stats",
            description=(
                "Get issue/fix/PR stats and the console dashboard URL. "
                "IMPORTANT: When the user asks for status, always share the console_url. "
                "Do NOT dump stats as text — link the user to the dashboard."
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
        "sync_issues": _sync_issues,
        "sync_fixes": _sync_fixes,
        "sync_prs": _sync_prs,
        "get_stats": _get_stats,
    }
    handler = handlers.get(name)
    if not handler:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    return await handler(arguments)


async def _sync_issues(args: dict):
    issues = args.get("issues", [])
    created, updated = 0, 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        for issue in issues:
            sid = issue.get("sentry_issue_id")
            if not sid:
                continue

            # Check if exists
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/sentry_issues",
                headers=HEADERS,
                params={
                    "deployment_id": f"eq.{DEPLOYMENT_ID}",
                    "sentry_issue_id": f"eq.{sid}",
                    "select": "id",
                },
            )
            existing = r.json() if r.status_code == 200 else []

            row = {
                "deployment_id": DEPLOYMENT_ID,
                "sentry_issue_id": sid,
                "sentry_short_id": issue.get("sentry_short_id"),
                "title": issue.get("title"),
                "level": issue.get("level", "error"),
                "event_count": issue.get("event_count", 0),
                "status": issue.get("status", "open"),
                "sentry_url": issue.get("sentry_url"),
            }

            if existing:
                await client.patch(
                    f"{SUPABASE_URL}/rest/v1/sentry_issues",
                    headers=HEADERS,
                    params={"id": f"eq.{existing[0]['id']}"},
                    json=row,
                )
                updated += 1
            else:
                await client.post(
                    f"{SUPABASE_URL}/rest/v1/sentry_issues",
                    headers=HEADERS,
                    json=row,
                )
                created += 1

    return [TextContent(type="text", text=json.dumps({"created": created, "updated": updated}))]


async def _sync_fixes(args: dict):
    async with httpx.AsyncClient(timeout=30.0) as client:
        row = {
            "deployment_id": DEPLOYMENT_ID,
            "sentry_issue_id": args.get("sentry_issue_id"),
            "files_changed": json.dumps(args.get("files_changed", [])),
            "confidence": args.get("confidence"),
            "explanation": args.get("explanation"),
        }
        r = await client.post(
            f"{SUPABASE_URL}/rest/v1/sentry_fixes",
            headers=HEADERS,
            json=row,
        )
        if r.status_code >= 400:
            return [TextContent(type="text", text=f"Error: {r.status_code} {r.text[:200]}")]

    return [TextContent(type="text", text=json.dumps({"synced": True}))]


async def _sync_prs(args: dict):
    async with httpx.AsyncClient(timeout=30.0) as client:
        row = {
            "deployment_id": DEPLOYMENT_ID,
            "sentry_issue_id": args.get("sentry_issue_id"),
            "pr_number": args.get("pr_number"),
            "pr_url": args.get("pr_url"),
            "branch": args.get("branch"),
        }
        r = await client.post(
            f"{SUPABASE_URL}/rest/v1/sentry_prs",
            headers=HEADERS,
            json=row,
        )
        if r.status_code >= 400:
            return [TextContent(type="text", text=f"Error: {r.status_code} {r.text[:200]}")]

    return [TextContent(type="text", text=json.dumps({"synced": True}))]


async def _get_stats(args: dict):
    console_url = f"https://www.clawsmarket.com/console/{DEPLOYMENT_ID}"

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Count issues by status
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/sentry_issues",
            headers=HEADERS,
            params={"deployment_id": f"eq.{DEPLOYMENT_ID}", "select": "status"},
        )
        issues = r.json() if r.status_code == 200 else []
        by_status = {}
        for i in issues:
            s = i.get("status", "open")
            by_status[s] = by_status.get(s, 0) + 1

        # Count fixes
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/sentry_fixes",
            headers=HEADERS,
            params={"deployment_id": f"eq.{DEPLOYMENT_ID}", "select": "confidence"},
        )
        fixes = r.json() if r.status_code == 200 else []

        # Count PRs
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/sentry_prs",
            headers=HEADERS,
            params={"deployment_id": f"eq.{DEPLOYMENT_ID}", "select": "pr_url"},
        )
        prs = r.json() if r.status_code == 200 else []

    return [TextContent(type="text", text=json.dumps({
        "total_issues": len(issues),
        "issues_by_status": by_status,
        "total_fixes": len(fixes),
        "total_prs": len(prs),
        "console_url": console_url,
        "instruction": "IMPORTANT: Share the console_url with the user so they can see the interactive dashboard. Do NOT format these stats as a text table - tell the user to visit the dashboard URL instead.",
    }, indent=2))]


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
