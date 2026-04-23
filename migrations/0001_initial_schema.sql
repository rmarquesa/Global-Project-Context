create extension if not exists vector;
create extension if not exists pgcrypto;

create table if not exists gpc_projects (
    id uuid primary key default gen_random_uuid(),
    slug text not null unique,
    name text not null,
    root_path text not null unique,
    description text,
    primary_language text,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (slug ~ '^[a-z0-9][a-z0-9_-]*$')
);

create table if not exists gpc_files (
    id uuid primary key default gen_random_uuid(),
    project_id uuid not null references gpc_projects(id) on delete cascade,
    relative_path text not null,
    absolute_path text not null,
    language text,
    file_type text not null default 'unknown',
    size_bytes bigint not null default 0,
    content_hash text,
    indexed_at timestamptz,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (project_id, relative_path)
);

create table if not exists gpc_chunks (
    id uuid primary key default gen_random_uuid(),
    project_id uuid not null references gpc_projects(id) on delete cascade,
    file_id uuid references gpc_files(id) on delete cascade,
    source_type text not null,
    chunk_type text not null,
    chunk_index integer not null default 0,
    title text,
    content text not null,
    content_hash text not null,
    token_count integer,
    qdrant_collection text not null default 'gpc_memory',
    qdrant_point_id text,
    embedding_model text,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (project_id, source_type, content_hash)
);

create table if not exists gpc_entities (
    id uuid primary key default gen_random_uuid(),
    project_id uuid references gpc_projects(id) on delete cascade,
    name text not null,
    entity_type text not null,
    external_ref text,
    description text,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (project_id, entity_type, name)
);

create table if not exists gpc_relations (
    id uuid primary key default gen_random_uuid(),
    project_id uuid references gpc_projects(id) on delete cascade,
    source_entity_id uuid not null references gpc_entities(id) on delete cascade,
    target_entity_id uuid not null references gpc_entities(id) on delete cascade,
    relation_type text not null,
    evidence_chunk_id uuid references gpc_chunks(id) on delete set null,
    confidence numeric(4, 3),
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    check (source_entity_id <> target_entity_id),
    check (confidence is null or (confidence >= 0 and confidence <= 1))
);

create table if not exists gpc_decisions (
    id uuid primary key default gen_random_uuid(),
    project_id uuid references gpc_projects(id) on delete cascade,
    title text not null,
    status text not null default 'active',
    content text not null,
    source_path text,
    decided_at timestamptz,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists gpc_index_runs (
    id uuid primary key default gen_random_uuid(),
    project_id uuid references gpc_projects(id) on delete cascade,
    status text not null,
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    files_seen integer not null default 0,
    files_indexed integer not null default 0,
    chunks_written integer not null default 0,
    error_message text,
    metadata jsonb not null default '{}'::jsonb,
    check (status in ('running', 'succeeded', 'failed', 'cancelled'))
);

create table if not exists gpc_events (
    id uuid primary key default gen_random_uuid(),
    project_id uuid references gpc_projects(id) on delete set null,
    event_type text not null,
    title text not null,
    body text,
    source text,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create or replace function gpc_touch_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists gpc_projects_touch_updated_at on gpc_projects;
create trigger gpc_projects_touch_updated_at
before update on gpc_projects
for each row execute function gpc_touch_updated_at();

drop trigger if exists gpc_files_touch_updated_at on gpc_files;
create trigger gpc_files_touch_updated_at
before update on gpc_files
for each row execute function gpc_touch_updated_at();

drop trigger if exists gpc_chunks_touch_updated_at on gpc_chunks;
create trigger gpc_chunks_touch_updated_at
before update on gpc_chunks
for each row execute function gpc_touch_updated_at();

drop trigger if exists gpc_entities_touch_updated_at on gpc_entities;
create trigger gpc_entities_touch_updated_at
before update on gpc_entities
for each row execute function gpc_touch_updated_at();

drop trigger if exists gpc_decisions_touch_updated_at on gpc_decisions;
create trigger gpc_decisions_touch_updated_at
before update on gpc_decisions
for each row execute function gpc_touch_updated_at();

create index if not exists gpc_files_project_path_idx
on gpc_files(project_id, relative_path);

create index if not exists gpc_files_hash_idx
on gpc_files(content_hash);

create index if not exists gpc_chunks_project_type_idx
on gpc_chunks(project_id, source_type, chunk_type);

create index if not exists gpc_chunks_qdrant_point_idx
on gpc_chunks(qdrant_collection, qdrant_point_id);

create index if not exists gpc_chunks_metadata_idx
on gpc_chunks using gin(metadata);

create index if not exists gpc_entities_project_type_name_idx
on gpc_entities(project_id, entity_type, name);

create index if not exists gpc_relations_project_type_idx
on gpc_relations(project_id, relation_type);

create index if not exists gpc_events_project_type_idx
on gpc_events(project_id, event_type, created_at desc);
