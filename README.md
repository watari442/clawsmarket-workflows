# ClawsMarket Workflows

Workflow templates for the ClawsMarket marketplace. Each workflow defines the agent behavior, tools, prompts, and configuration schema for a deployment type.

## Structure

```
workflows/
└── {workflow-slug}/
    ├── SKILL.md              # Core agent instructions (injected into system prompt)
    ├── schema.json           # Config schema (validated on PATCH /config)
    ├── dashboard.json        # Dashboard tab/widget definitions
    ├── workspace/            # Per-deployment workspace templates
    │   ├── SOUL.md           # Agent personality + dashboard instructions
    │   └── AGENTS.md         # Agent guidelines + behavioral rules
    ├── prompts/              # Reusable prompt templates
    │   └── *.md
    └── tools/                # MCP server implementations
        └── *.py
```

## Template Variables

Workspace files (`workspace/SOUL.md`, `workspace/AGENTS.md`) support template variables that get replaced at deployment time:

| Variable | Replaced With |
|---|---|
| `{{DEPLOYMENT_ID}}` | The deployment's UUID |

## How It Works

1. **New deployment** — Provisioner copies `workflows/{slug}/` into the deployment's workspace, replacing template variables
2. **User customization** — The deployed agent (or user) can modify their copy of SOUL.md, AGENTS.md, MEMORY.md freely
3. **Reset** — `POST /api/deployments/{id}/workspace/reset` restores SKILL.md, tools, and prompts from this template (preserves memory/)
4. **Template updates** — Changes here propagate to new deployments. Existing deployments keep their copy unless reset.

## Adding a Workflow

1. Create `workflows/{your-slug}/`
2. Add at minimum: `SKILL.md`, `schema.json`
3. Add `workspace/SOUL.md` with personality and `{{DEPLOYMENT_ID}}` for the console URL
4. Add `tools/` with MCP servers if the workflow needs external integrations
5. Add `prompts/` for any reusable prompt templates
6. Submit a PR — all workflows go through review before going live

## Active Workflows

| Slug | Status | Description |
|---|---|---|
| `ai-sdr` | Live | Sales development: lead sourcing, email outreach, campaign management |
