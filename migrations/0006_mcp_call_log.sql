-- Log every MCP tool invocation so we can audit whether AI clients are
-- actually using the server. This answers "is Claude / Codex hitting the
-- MCP?" with data instead of intuition.
--
-- Intentional design:
--   * one row per tool call, even on failure, so we can see rejected calls;
--   * every row references the resolved project when there is one, set null
--     when the call never reached project resolution;
--   * args and result metadata are stored as jsonb for flexible querying,
--     but the ``args`` column excludes large payloads (query text is kept,
--     full file contents are not) to avoid log bloat;
--   * retention is the operator's responsibility — keep the table small
--     with a cron DELETE if needed.

create table if not exists gpc_mcp_calls (
    id uuid primary key default gen_random_uuid(),
    called_at timestamptz not null default now(),
    tool text not null,
    project_id uuid references gpc_projects(id) on delete set null,
    project_slug text,
    repo_slug text,
    client_name text,
    client_cwd text,
    duration_ms integer,
    success boolean not null default true,
    error_type text,
    error_message text,
    args jsonb not null default '{}'::jsonb,
    result_meta jsonb not null default '{}'::jsonb
);

create index if not exists gpc_mcp_calls_called_at_idx
    on gpc_mcp_calls (called_at desc);

create index if not exists gpc_mcp_calls_tool_idx
    on gpc_mcp_calls (tool, called_at desc);

create index if not exists gpc_mcp_calls_project_idx
    on gpc_mcp_calls (project_id, called_at desc);

create index if not exists gpc_mcp_calls_client_idx
    on gpc_mcp_calls (client_name, called_at desc);
