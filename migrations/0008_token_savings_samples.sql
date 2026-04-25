create table if not exists gpc_token_savings_samples (
    id uuid primary key default gen_random_uuid(),
    created_at timestamptz not null default now(),
    mcp_call_id uuid references gpc_mcp_calls(id) on delete set null,
    tool text not null,
    project_id uuid references gpc_projects(id) on delete cascade,
    project_slug text not null,
    repo_slug text,
    query text,
    indexed_tokens integer not null default 0,
    retrieved_tokens integer not null default 0,
    saved_tokens integer not null default 0,
    savings_percent numeric(6, 2) not null default 0,
    returned_chars integer not null default 0,
    max_chunks integer,
    max_chars integer,
    result_count integer,
    client_name text,
    metadata jsonb not null default '{}'::jsonb
);

create index if not exists gpc_token_savings_samples_project_time_idx
    on gpc_token_savings_samples (project_id, created_at desc);

create index if not exists gpc_token_savings_samples_project_slug_time_idx
    on gpc_token_savings_samples (project_slug, created_at desc);

create index if not exists gpc_token_savings_samples_tool_time_idx
    on gpc_token_savings_samples (tool, created_at desc);

create index if not exists gpc_token_savings_samples_repo_time_idx
    on gpc_token_savings_samples (repo_slug, created_at desc)
    where repo_slug is not null;

create index if not exists gpc_token_savings_samples_created_at_idx
    on gpc_token_savings_samples (created_at desc);
