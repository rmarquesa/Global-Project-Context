-- Introduce repositories as first-class citizens under a project.
--
-- Before this migration, each local repository was registered as its own
-- project. That prevented the MCP layer from treating a logical product
-- (e.g. "alugafacil") with multiple repositories (workers-gateway,
-- workers-users, web, ...) as a single entity.
--
-- After this migration, every project has at least one repository, and files
-- / chunks are owned by both a project and a repo. Existing data is
-- backfilled to a default repo per project so that behavior is preserved for
-- callers that have not yet adopted the new model.

create table if not exists gpc_repos (
    id uuid primary key default gen_random_uuid(),
    project_id uuid not null references gpc_projects(id) on delete cascade,
    slug text not null,
    name text,
    root_path text not null unique,
    description text,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (project_id, slug),
    check (slug ~ '^[a-z0-9][a-z0-9_-]*$')
);

drop trigger if exists gpc_repos_touch_updated_at on gpc_repos;
create trigger gpc_repos_touch_updated_at
before update on gpc_repos
for each row execute function gpc_touch_updated_at();

create index if not exists gpc_repos_project_idx on gpc_repos(project_id);

-- Backfill a default repo per existing project using the project root as the
-- repo root. Safe to re-run: on conflict, the row is left untouched.
insert into gpc_repos (project_id, slug, name, root_path, description)
select id, slug, name, root_path, description
from gpc_projects
on conflict (root_path) do nothing;

alter table gpc_files
    add column if not exists repo_id uuid references gpc_repos(id) on delete set null;

alter table gpc_chunks
    add column if not exists repo_id uuid references gpc_repos(id) on delete set null;

-- Link every existing file/chunk to the default repo of its project.
update gpc_files f
set repo_id = r.id
from gpc_repos r
where r.project_id = f.project_id
  and r.root_path = (select root_path from gpc_projects p where p.id = f.project_id)
  and f.repo_id is null;

update gpc_chunks c
set repo_id = r.id
from gpc_repos r
where r.project_id = c.project_id
  and r.root_path = (select root_path from gpc_projects p where p.id = c.project_id)
  and c.repo_id is null;

create index if not exists gpc_files_repo_idx on gpc_files(repo_id);
create index if not exists gpc_chunks_repo_idx on gpc_chunks(repo_id);
