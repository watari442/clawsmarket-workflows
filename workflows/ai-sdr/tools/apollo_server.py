#!/usr/bin/env python3
"""MCP server for Apollo.io — lead search and enrichment."""

import json
import os
import asyncio

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

API_KEY = os.environ.get("APOLLO_API_KEY", "")
API_URL = "https://api.apollo.io/api/v1"
HEADERS = {"X-Api-Key": API_KEY, "Content-Type": "application/json"}

server = Server("apollo")


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="search_leads",
            description=(
                "Search Apollo.io for people matching criteria. FREE — no credits used. "
                "Returns name, title, company, industry, size, location. "
                "Does NOT return email addresses (use enrich_lead for that)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "person_titles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Job titles to search for, e.g. ['CEO', 'Founder', 'CTO']",
                    },
                    "employee_ranges": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Company size ranges, e.g. ['11-50', '51-200', '201-500']",
                    },
                    "locations": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Person locations, e.g. ['United States', 'California']",
                    },
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Organization keyword tags to filter by",
                    },
                    "page": {
                        "type": "integer",
                        "description": "Page number (1-indexed). Default: 1",
                        "default": 1,
                    },
                    "per_page": {
                        "type": "integer",
                        "description": "Results per page (max 100). Default: 25",
                        "default": 25,
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="enrich_lead",
            description=(
                "Enrich a lead by Apollo person ID to get their email address, "
                "LinkedIn URL, and full company details. COSTS 1 CREDIT per call — "
                "only enrich leads you have already qualified."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "apollo_id": {
                        "type": "string",
                        "description": "Apollo person ID from search results",
                    },
                },
                "required": ["apollo_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if not API_KEY:
        return [TextContent(type="text", text="Error: APOLLO_API_KEY not configured")]

    if name == "search_leads":
        return await _search(arguments)
    elif name == "enrich_lead":
        return await _enrich(arguments)
    return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def _search(args: dict):
    body = {"per_page": args.get("per_page", 25), "page": args.get("page", 1)}
    if titles := args.get("person_titles"):
        body["person_titles"] = titles
    if ranges := args.get("employee_ranges"):
        body["organization_num_employees_ranges"] = ranges
    if locs := args.get("locations"):
        body["person_locations"] = locs
    if kw := args.get("keywords"):
        body["q_organization_keyword_tags"] = kw

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{API_URL}/mixed_people/api_search", headers=HEADERS, json=body)
            r.raise_for_status()
    except Exception as e:
        return [TextContent(type="text", text=f"Apollo search error: {e}")]

    data = r.json()
    people = data.get("people", [])
    if not people:
        return [TextContent(type="text", text="No results found.")]

    results = []
    for p in people:
        org = p.get("organization") or {}
        results.append({
            "apollo_id": p.get("id"),
            "name": f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
            "title": p.get("title", ""),
            "company": org.get("name", ""),
            "industry": org.get("industry", ""),
            "employees": org.get("estimated_num_employees"),
            "city": p.get("city", ""),
            "state": p.get("state", ""),
            "country": p.get("country", ""),
            "linkedin": p.get("linkedin_url", ""),
        })

    return [TextContent(type="text", text=json.dumps({
        "count": len(results),
        "page": args.get("page", 1),
        "leads": results,
    }, indent=2))]


async def _enrich(args: dict):
    apollo_id = args.get("apollo_id")
    if not apollo_id:
        return [TextContent(type="text", text="Error: apollo_id is required")]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{API_URL}/people/match",
                headers=HEADERS,
                json={"id": apollo_id, "reveal_personal_emails": False},
            )
            r.raise_for_status()
    except Exception as e:
        return [TextContent(type="text", text=f"Apollo enrich error: {e}")]

    person = r.json().get("person", {})
    if not person:
        return [TextContent(type="text", text="No person data returned")]

    org = person.get("organization") or {}
    return [TextContent(type="text", text=json.dumps({
        "apollo_id": person.get("id"),
        "name": f"{person.get('first_name', '')} {person.get('last_name', '')}".strip(),
        "email": person.get("email", ""),
        "title": person.get("title", ""),
        "company": org.get("name", ""),
        "employees": org.get("estimated_num_employees"),
        "linkedin": person.get("linkedin_url", ""),
        "city": person.get("city", ""),
        "state": person.get("state", ""),
    }, indent=2))]


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
