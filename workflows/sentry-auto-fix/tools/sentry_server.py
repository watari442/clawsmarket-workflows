#!/usr/bin/env python3
"""MCP server for Sentry API — poll issues and fetch event details.

Env vars:
  SENTRY_AUTH_TOKEN  — Sentry auth token (project:read, event:read scopes)
  SENTRY_ORG         — Sentry org slug
  SENTRY_PROJECT     — Sentry project slug
  SENTRY_API_BASE    — API base URL (default: https://sentry.io/api/0)
"""

import asyncio
import json
import os
import time

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

SENTRY_AUTH_TOKEN = os.environ.get("SENTRY_AUTH_TOKEN", "")
SENTRY_ORG = os.environ.get("SENTRY_ORG", "")
SENTRY_PROJECT = os.environ.get("SENTRY_PROJECT", "")
SENTRY_API_BASE = os.environ.get("SENTRY_API_BASE", "https://sentry.io/api/0")

server = Server("sentry")


def _headers():
    return {
        "Authorization": f"Bearer {SENTRY_AUTH_TOKEN}",
        "Content-Type": "application/json",
    }


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="poll_issues",
            description="Fetch unresolved issues from Sentry, sorted by most recent. Returns title, severity, event count, user count, and Sentry URL for each issue.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max issues to fetch (default 25)", "default": 25},
                    "query": {"type": "string", "description": "Sentry search query (default: is:unresolved)", "default": "is:unresolved"},
                },
            },
        ),
        Tool(
            name="get_event",
            description="Fetch the latest event for a Sentry issue. Returns full stack trace, breadcrumbs, and context. Use this to analyze an error before generating a fix.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Sentry issue ID"},
                },
                "required": ["issue_id"],
            },
        ),
        Tool(
            name="get_issue",
            description="Fetch full details for a Sentry issue (first/last seen, user count, tags, status).",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Sentry issue ID"},
                },
                "required": ["issue_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if not SENTRY_AUTH_TOKEN:
        return [TextContent(type="text", text="Error: SENTRY_AUTH_TOKEN not configured")]
    if not SENTRY_ORG or not SENTRY_PROJECT:
        return [TextContent(type="text", text="Error: SENTRY_ORG/SENTRY_PROJECT not configured")]

    handlers = {
        "poll_issues": _poll_issues,
        "get_event": _get_event,
        "get_issue": _get_issue,
    }
    handler = handlers.get(name)
    if not handler:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    return await handler(arguments)


async def _poll_issues(args: dict):
    limit = args.get("limit", 25)
    query = args.get("query", "is:unresolved")

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{SENTRY_API_BASE}/projects/{SENTRY_ORG}/{SENTRY_PROJECT}/issues/",
            headers=_headers(),
            params={"query": query, "sort": "date", "limit": limit},
        )
        if resp.status_code != 200:
            return [TextContent(type="text", text=f"Sentry API error {resp.status_code}: {resp.text[:300]}")]

        issues = resp.json()

    results = []
    for issue in issues:
        results.append({
            "id": issue.get("id"),
            "short_id": issue.get("shortId"),
            "title": issue.get("title"),
            "level": issue.get("level"),
            "count": issue.get("count"),
            "user_count": issue.get("userCount"),
            "first_seen": issue.get("firstSeen"),
            "last_seen": issue.get("lastSeen"),
            "status": issue.get("status"),
            "url": issue.get("permalink"),
        })

    return [TextContent(type="text", text=json.dumps({
        "total": len(results),
        "issues": results,
    }, indent=2))]


async def _get_event(args: dict):
    issue_id = args.get("issue_id")
    if not issue_id:
        return [TextContent(type="text", text="Error: issue_id required")]

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{SENTRY_API_BASE}/issues/{issue_id}/events/latest/",
            headers=_headers(),
        )
        if resp.status_code != 200:
            return [TextContent(type="text", text=f"Sentry API error {resp.status_code}: {resp.text[:300]}")]

        event = resp.json()

    # Extract readable stack trace
    stacktrace_parts = []
    file_paths = []
    for entry in event.get("entries", []):
        if entry.get("type") == "exception":
            for value in entry.get("data", {}).get("values", []):
                exc_type = value.get("type", "Unknown")
                exc_value = value.get("value", "")
                stacktrace_parts.append(f"Exception: {exc_type}: {exc_value}")

                st = value.get("stacktrace")
                if st:
                    for frame in st.get("frames", []):
                        filename = frame.get("filename", "?")
                        lineno = frame.get("lineNo", "?")
                        function = frame.get("function", "?")
                        stacktrace_parts.append(f"  {filename}:{lineno} in {function}")
                        if filename and "node_modules" not in filename and not filename.startswith("node:"):
                            file_paths.append(filename)

        elif entry.get("type") == "breadcrumbs":
            crumbs = entry.get("data", {}).get("values", [])[-10:]
            if crumbs:
                stacktrace_parts.append("\nBreadcrumbs (last 10):")
                for c in crumbs:
                    stacktrace_parts.append(f"  [{c.get('category','')}] {c.get('message','')} ({c.get('level','')})")

    return [TextContent(type="text", text=json.dumps({
        "event_id": event.get("eventID"),
        "stacktrace": "\n".join(stacktrace_parts),
        "file_paths": list(set(file_paths)),
        "tags": {t["key"]: t["value"] for t in event.get("tags", [])},
    }, indent=2))]


async def _get_issue(args: dict):
    issue_id = args.get("issue_id")
    if not issue_id:
        return [TextContent(type="text", text="Error: issue_id required")]

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{SENTRY_API_BASE}/issues/{issue_id}/",
            headers=_headers(),
        )
        if resp.status_code != 200:
            return [TextContent(type="text", text=f"Sentry API error {resp.status_code}: {resp.text[:300]}")]

        issue = resp.json()

    return [TextContent(type="text", text=json.dumps({
        "id": issue.get("id"),
        "short_id": issue.get("shortId"),
        "title": issue.get("title"),
        "culprit": issue.get("culprit"),
        "level": issue.get("level"),
        "status": issue.get("status"),
        "count": issue.get("count"),
        "user_count": issue.get("userCount"),
        "first_seen": issue.get("firstSeen"),
        "last_seen": issue.get("lastSeen"),
        "url": issue.get("permalink"),
    }, indent=2))]


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
