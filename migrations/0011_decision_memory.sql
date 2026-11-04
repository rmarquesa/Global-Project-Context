-- Explicit human-curated decisions. Apply after 0004 (repos) and 0005 (file
-- identity), in the migration runner's transaction. No source or Git lookup is
-- performed here. Existing decisions have no verified evidence or approval.
alter table gpc_decisions
    add column if not exists repo_id uuid references gpc_repos(id) on delete set null,
    add column if not exists decision_key text,
    add column if not exists source_hash text,
    add column if not exists source_revision text,
    add column if not exists source_file_id uuid references gpc_files(id) on delete set null,
    add column if not exists superseded_by uuid references gpc_decisions(id) on delete set null,
    add column if not exists approved_by text,
    add column if not exists approved_at timestamptz,
    add column if not exists approval_reason text;

alter table gpc_decisions alter column status set default 'pending';
update gpc_decisions set status = 'legacy' where status = 'active';

-- Nullable legacy scope/evidence is deliberately not backfilled or endorsed.
-- Keys identify explicit propositions, not semantic contradiction detection.
create unique index if not exists gpc_decisions_approved_scope_key_uidx
    on gpc_decisions(project_id, repo_id, source_path, decision_key)
    where status = 'approved';

create table if not exists gpc_decision_events (
    id uuid primary key default gen_random_uuid(),
    decision_id uuid not null references gpc_decisions(id) on delete cascade,
    action text not null check (action in ('captured', 'approved', 'invalidated', 'superseded')),
    actor text not null,
    reason text,
    created_at timestamptz not null default now()
);
create index if not exists gpc_decision_events_decision_idx
    on gpc_decision_events(decision_id, created_at, id);
