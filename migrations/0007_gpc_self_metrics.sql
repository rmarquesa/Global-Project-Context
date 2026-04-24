-- Longitudinal metrics for the GPC graph itself.
--
-- Every ``gpc-index`` and ``gpc graph-bridge`` run writes one row here so we
-- can answer "did the graph drift?" with data. The collector also runs on
-- demand (``gpc metrics collect`` or the ``gpc.graph_diff`` MCP tool when
-- there is no recent snapshot).
--
-- Design:
--   * one row per project per run; snapshots are immutable;
--   * ``source`` identifies the trigger (gpc-index, graph-bridge, manual, snapshot);
--   * numeric counts stay as columns for cheap aggregation over many rows;
--   * ``god_nodes_top10`` / ``confidence_distribution`` / ``metadata`` stay as
--     jsonb so future collectors can add signals without another migration.

create table if not exists gpc_self_metrics (
    id uuid primary key default gen_random_uuid(),
    collected_at timestamptz not null default now(),
    project_id uuid references gpc_projects(id) on delete cascade,
    project_slug text not null,
    source text not null,

    files_count integer,
    chunks_count integer,
    entities_count integer,
    relations_count integer,

    graphify_projects integer,
    graphify_repos integer,
    graphify_nodes integer,
    graphify_edges_same_repo integer,
    graphify_edges_cross_repo integer,
    cross_repo_bridges integer,

    extracted_count integer,
    inferred_count integer,
    ambiguous_count integer,

    weakly_connected_nodes integer,
    community_count integer,
    god_nodes_top10 jsonb not null default '[]'::jsonb,

    max_file_age_hours integer,

    metadata jsonb not null default '{}'::jsonb,

    check (source ~ '^[a-z0-9][a-z0-9_-]*$')
);

create index if not exists gpc_self_metrics_project_time_idx
    on gpc_self_metrics (project_id, collected_at desc);

create index if not exists gpc_self_metrics_slug_time_idx
    on gpc_self_metrics (project_slug, collected_at desc);

create index if not exists gpc_self_metrics_source_time_idx
    on gpc_self_metrics (source, collected_at desc);
