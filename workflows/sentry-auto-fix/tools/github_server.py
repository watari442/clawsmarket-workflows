#!/usr/bin/env python3
"""MCP server for GitHub operations — sync repo, apply fixes, create PRs.

Env vars:
  GITHUB_TOKEN       — GitHub PAT with repo scope
  GITHUB_REPO        — owner/repo format
  GITHUB_CLONE_PATH  — Local checkout path
  GITHUB_BASE_BRANCH — Base branch (default: main)
"""

import asyncio
import json
import os
import subprocess
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
GITHUB_CLONE_PATH = Path(os.environ.get("GITHUB_CLONE_PATH", ""))
GITHUB_BASE_BRANCH = os.environ.get("GITHUB_BASE_BRANCH", "main")

server = Server("github")


def _run_git(args: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=str(GITHUB_CLONE_PATH),
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        return result.returncode == 0, output
    except Exception as e:
        return False, str(e)


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="sync_repo",
            description="Pull latest changes from the remote repo. Run this before creating a fix branch.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="read_source",
            description="Read a source file from the local repo checkout. Use to inspect code referenced in stack traces.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path from repo root"},
                    "max_lines": {"type": "integer", "description": "Max lines to read (default 500)", "default": 500},
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="create_pr",
            description=(
                "Create a fix branch, apply file changes, commit, push, and open a GitHub PR. "
                "IMPORTANT: Always get user approval before calling this. "
                "Returns the PR URL on success."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "short_id": {"type": "string", "description": "Sentry short ID (e.g. PROJ-123) — used for branch name"},
                    "title": {"type": "string", "description": "Issue title for the PR"},
                    "sentry_url": {"type": "string", "description": "Link to the Sentry issue"},
                    "explanation": {"type": "string", "description": "Explanation of the fix"},
                    "confidence": {"type": "string", "description": "Fix confidence: high, medium, or low"},
                    "files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "old": {"type": "string", "description": "Exact text to replace"},
                                "new": {"type": "string", "description": "Replacement text"},
                            },
                            "required": ["path", "old", "new"],
                        },
                        "description": "File changes to apply",
                    },
                },
                "required": ["short_id", "title", "files"],
            },
        ),
        Tool(
            name="list_fix_prs",
            description="List open sentry-fix/* PRs with their status.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if not GITHUB_CLONE_PATH or not GITHUB_CLONE_PATH.exists():
        return [TextContent(type="text", text=f"Error: GITHUB_CLONE_PATH not found: {GITHUB_CLONE_PATH}")]

    handlers = {
        "sync_repo": _sync_repo,
        "read_source": _read_source,
        "create_pr": _create_pr,
        "list_fix_prs": _list_fix_prs,
    }
    handler = handlers.get(name)
    if not handler:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    return await handler(arguments)


async def _sync_repo(args: dict):
    ok, out = _run_git(["fetch", "origin"])
    if not ok:
        return [TextContent(type="text", text=f"fetch failed: {out}")]
    ok, out = _run_git(["checkout", GITHUB_BASE_BRANCH])
    if not ok:
        return [TextContent(type="text", text=f"checkout failed: {out}")]
    ok, out = _run_git(["pull", "--rebase", "origin", GITHUB_BASE_BRANCH])
    if not ok:
        return [TextContent(type="text", text=f"pull failed: {out}")]
    return [TextContent(type="text", text=json.dumps({"synced": True, "branch": GITHUB_BASE_BRANCH}))]


async def _read_source(args: dict):
    path = args.get("path", "")
    max_lines = args.get("max_lines", 500)

    # Normalize Sentry paths
    for prefix in ["/vercel/path0/", "/var/task/", ".next/server/"]:
        if path.startswith(prefix):
            path = path[len(prefix):]
    path = path.lstrip("/")

    full_path = GITHUB_CLONE_PATH / path
    if not full_path.exists():
        # Try common alternatives
        for alt in [GITHUB_CLONE_PATH / "src" / path]:
            if alt.exists():
                full_path = alt
                break
        else:
            return [TextContent(type="text", text=f"File not found: {path}")]

    try:
        lines = full_path.read_text(encoding="utf-8").splitlines()
        if len(lines) > max_lines:
            lines = lines[:max_lines] + [f"... (truncated at {max_lines} lines)"]
        return [TextContent(type="text", text="\n".join(lines))]
    except Exception as e:
        return [TextContent(type="text", text=f"Error reading {path}: {e}")]


async def _create_pr(args: dict):
    short_id = args.get("short_id", "unknown")
    title = args.get("title", "Sentry fix")
    sentry_url = args.get("sentry_url", "")
    explanation = args.get("explanation", "")
    confidence = args.get("confidence", "medium")
    files = args.get("files", [])

    if not files:
        return [TextContent(type="text", text="Error: no file changes provided")]

    branch = f"sentry-fix/{short_id}"

    # Create branch
    _run_git(["checkout", GITHUB_BASE_BRANCH])
    ok, out = _run_git(["checkout", "-b", branch])
    if not ok:
        ok, _ = _run_git(["checkout", branch])
        if ok:
            _run_git(["reset", "--hard", f"origin/{GITHUB_BASE_BRANCH}"])
        else:
            return [TextContent(type="text", text=f"Failed to create branch: {out}")]

    # Apply changes
    modified = []
    for change in files:
        filepath = GITHUB_CLONE_PATH / change["path"]
        if not filepath.exists():
            continue
        content = filepath.read_text(encoding="utf-8")
        if change["old"] not in content:
            continue
        if content.count(change["old"]) > 1:
            continue
        filepath.write_text(content.replace(change["old"], change["new"], 1), encoding="utf-8")
        modified.append(change["path"])

    if not modified:
        _run_git(["checkout", GITHUB_BASE_BRANCH])
        return [TextContent(type="text", text="Error: no files were modified (OLD blocks didn't match source)")]

    # Commit and push
    _run_git(["add", "-A"])
    commit_msg = (
        f"fix: {title}\n\n"
        f"Auto-fix by Patchwork (confidence: {confidence})\n"
        f"Sentry: {sentry_url}\n\n"
        f"{explanation}\n\n"
        f"Co-Authored-By: Patchwork <noreply@clawsmarket.com>"
    )
    ok, out = _run_git(["commit", "-m", commit_msg])
    if not ok:
        _run_git(["checkout", GITHUB_BASE_BRANCH])
        return [TextContent(type="text", text=f"Commit failed: {out}")]

    ok, out = _run_git(["push", "-u", "origin", branch])
    if not ok:
        _run_git(["checkout", GITHUB_BASE_BRANCH])
        return [TextContent(type="text", text=f"Push failed: {out}")]

    # Create PR via gh CLI
    pr_title = f"fix: {title}"[:70]
    pr_body = (
        f"## Sentry Auto-Fix\n\n"
        f"**Issue**: [{title}]({sentry_url})\n"
        f"**Confidence**: {confidence}\n\n"
        f"**Explanation**: {explanation}\n\n"
        f"**Files changed**: {', '.join(modified)}\n\n"
        f"---\n"
        f"Generated by Patchwork (Sentry Auto-Fix)"
    )

    try:
        result = subprocess.run(
            ["gh", "pr", "create", "--repo", GITHUB_REPO,
             "--base", GITHUB_BASE_BRANCH, "--head", branch,
             "--title", pr_title, "--body", pr_body],
            capture_output=True, text=True, timeout=60,
        )
        _run_git(["checkout", GITHUB_BASE_BRANCH])

        if result.returncode != 0:
            return [TextContent(type="text", text=f"PR creation failed: {result.stderr}")]

        pr_url = result.stdout.strip()
        return [TextContent(type="text", text=json.dumps({
            "pr_url": pr_url,
            "branch": branch,
            "files_modified": modified,
            "confidence": confidence,
        }, indent=2))]
    except Exception as e:
        _run_git(["checkout", GITHUB_BASE_BRANCH])
        return [TextContent(type="text", text=f"PR creation error: {e}")]


async def _list_fix_prs(args: dict):
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", GITHUB_REPO,
             "--head", "sentry-fix/", "--json", "number,title,url,state,headRefName"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return [TextContent(type="text", text=f"Error: {result.stderr}")]
        return [TextContent(type="text", text=result.stdout)]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
