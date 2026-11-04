-- Optional acceleration for lexical candidates; no embedding service required.
-- This expression must match gpc.retrieval.LEXICAL_VECTOR exactly (sans alias).
create index if not exists gpc_chunks_lexical_content_idx
    on gpc_chunks using gin (
        to_tsvector('simple'::regconfig, coalesce(title, '') || ' ' || content)
    )
    where source_type = 'project_file';
