create table if not exists gpc_graph_projections (
    id uuid primary key default gen_random_uuid(),
    projection_name text not null,
    target_system text not null default 'neo4j',
    status text not null,
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    projects_written integer not null default 0,
    entities_written integer not null default 0,
    relations_written integer not null default 0,
    error_message text,
    metadata jsonb not null default '{}'::jsonb,
    check (projection_name ~ '^[a-z0-9][a-z0-9_-]*$'),
    check (target_system in ('neo4j')),
    check (status in ('running', 'succeeded', 'failed', 'cancelled'))
);

create index if not exists gpc_graph_projections_status_idx
on gpc_graph_projections(target_system, status, started_at desc);

create index if not exists gpc_graph_projections_name_idx
on gpc_graph_projections(projection_name, started_at desc);
