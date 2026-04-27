create table if not exists gpc_drift_signals (
    id uuid primary key default gen_random_uuid(),
    created_at timestamptz not null default now(),
    project_id uuid references gpc_projects(id) on delete cascade,
    project_slug text not null,
    severity text not null,
    signal_type text not null,
    message text not null,
    from_snapshot_id uuid references gpc_self_metrics(id) on delete set null,
    to_snapshot_id uuid references gpc_self_metrics(id) on delete set null,
    evidence jsonb not null default '{}'::jsonb,
    resolved_at timestamptz,
    check (severity in ('info', 'warning', 'critical')),
    check (signal_type ~ '^[a-z0-9][a-z0-9_-]*$')
);

create index if not exists gpc_drift_signals_project_time_idx
    on gpc_drift_signals (project_id, created_at desc);

create index if not exists gpc_drift_signals_slug_time_idx
    on gpc_drift_signals (project_slug, created_at desc);

create index if not exists gpc_drift_signals_type_time_idx
    on gpc_drift_signals (signal_type, created_at desc);

create index if not exists gpc_drift_signals_unresolved_idx
    on gpc_drift_signals (project_slug, severity, created_at desc)
    where resolved_at is null;
