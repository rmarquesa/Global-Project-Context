create table if not exists gpc_project_aliases (
    id uuid primary key default gen_random_uuid(),
    project_id uuid not null references gpc_projects(id) on delete cascade,
    alias text not null unique,
    alias_type text not null default 'manual',
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (alias ~ '^[a-z0-9][a-z0-9_-]*$'),
    check (alias_type ~ '^[a-z0-9][a-z0-9_-]*$')
);

create table if not exists gpc_sources (
    id uuid primary key default gen_random_uuid(),
    slug text not null unique,
    name text not null,
    source_type text not null,
    root_path text not null unique,
    description text,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (slug ~ '^[a-z0-9][a-z0-9_-]*$'),
    check (
        source_type in (
            'project_root',
            'obsidian_vault',
            'graphify_output',
            'documentation',
            'research',
            'other'
        )
    )
);

create table if not exists gpc_project_sources (
    project_id uuid not null references gpc_projects(id) on delete cascade,
    source_id uuid not null references gpc_sources(id) on delete cascade,
    role text not null default 'primary',
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (project_id, source_id),
    check (role ~ '^[a-z0-9][a-z0-9_-]*$')
);

drop trigger if exists gpc_project_aliases_touch_updated_at on gpc_project_aliases;
create trigger gpc_project_aliases_touch_updated_at
before update on gpc_project_aliases
for each row execute function gpc_touch_updated_at();

drop trigger if exists gpc_sources_touch_updated_at on gpc_sources;
create trigger gpc_sources_touch_updated_at
before update on gpc_sources
for each row execute function gpc_touch_updated_at();

drop trigger if exists gpc_project_sources_touch_updated_at on gpc_project_sources;
create trigger gpc_project_sources_touch_updated_at
before update on gpc_project_sources
for each row execute function gpc_touch_updated_at();

create index if not exists gpc_project_aliases_project_idx
on gpc_project_aliases(project_id);

create index if not exists gpc_sources_type_idx
on gpc_sources(source_type);

create index if not exists gpc_project_sources_source_idx
on gpc_project_sources(source_id);

create index if not exists gpc_project_sources_role_idx
on gpc_project_sources(project_id, role);
