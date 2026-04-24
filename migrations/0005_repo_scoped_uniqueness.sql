-- Expand uniqueness constraints to include repo_id so that multiple repos
-- under the same project can coexist without path or content-hash collisions.
--
-- The original 0001 constraints (project_id, relative_path) and
-- (project_id, source_type, content_hash) treated a project as a single
-- filesystem tree. With gpc_repos every repo has its own tree and can
-- legitimately contain a file with the same relative_path (e.g. every
-- Cloudflare worker has its own ``src/index.ts``).

alter table gpc_files
    drop constraint if exists gpc_files_project_id_relative_path_key;

create unique index if not exists gpc_files_project_repo_path_idx
    on gpc_files (project_id, coalesce(repo_id, '00000000-0000-0000-0000-000000000000'::uuid), relative_path);

alter table gpc_chunks
    drop constraint if exists gpc_chunks_project_id_source_type_content_hash_key;

create unique index if not exists gpc_chunks_project_repo_source_hash_idx
    on gpc_chunks (
        project_id,
        coalesce(repo_id, '00000000-0000-0000-0000-000000000000'::uuid),
        source_type,
        content_hash
    );
