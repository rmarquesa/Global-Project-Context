# Maintainer Notes

Internal checklist and conventions for maintainers of GPC. Not linked from
the public README.

## Pre-Release Checklist

Before tagging a release or making the repository public:

1. **Untracked sensitive files**. Run `git status` and verify nothing in
   `.env`, `*.bak`, `data/`, `graphify-out/`, `.gpc/notes/`, or local
   backups is staged. All of these are gitignored, but a manual confirmation
   is cheap.
2. **License files**. Confirm `LICENSE` and `NOTICE.md` are present.
   GPC uses `AGPL-3.0-or-later`.
3. **Example configs use placeholders**. `mcp_config.example.json` and
   `.env.example` must not contain local absolute paths or machine-specific
   secrets.
4. **Default service credentials**. The Docker compose passwords in
   `.env.example` are development defaults. Document this in
   `SECURITY.md` (already covered, re-verify on release).
5. **Smoke tests pass**. Run the full smoke suite against a fresh local
   stack (see [`docs/operations.md`](../docs/operations.md#validate)).
6. **Landing page build**. Open `site/index.html` and verify icons and fonts
   load (the page depends on the `simple-icons` jsDelivr CDN and Google
   Fonts).
7. **Internal docs are not linked publicly**. `.github/MAINTAINING.md`,
   `.gpc/notes/CONTEXT.md` and `.gpc/notes/MEMORY.md` are intentionally not
   linked from `README.md`.

## Privacy Conventions

| File / directory | Purpose | Tracked by Git? |
|---|---|---|
| `AGENTS.md` | Local agent instructions for tools running in this repo. | No — listed in `.gitignore`. |
| `.gpc/` | Per-project GPC runtime state and indexer logs. | No — listed in `.gitignore`. |
| `.gpc/notes/CONTEXT.md` | Long-form project context for AI assistants. | No. |
| `.gpc/notes/MEMORY.md` | Compact per-session memory for AI assistants. | No. |
| `graphify-out/` | Generated Graphify graphs. | No. |

These are local context for the maintainer's tooling. They should not be
moved into `docs/` or referenced from public documentation.

## Documentation Layout

Single source of truth for each topic:

| Topic | Canonical doc |
|---|---|
| MCP tool reference, client setup, troubleshooting | [`docs/mcp-clients.md`](../docs/mcp-clients.md) |
| Graphify hook, Neo4j consolidation | [`docs/graphify.md`](../docs/graphify.md) |
| GPC auto-index hooks | [`docs/automation.md`](../docs/automation.md) |
| Install, validate, reset, manual indexing | [`docs/operations.md`](../docs/operations.md) |
| Component overview and data flow | [`docs/architecture.md`](../docs/architecture.md) |
| Token-savings metric | [`docs/token-economy.md`](../docs/token-economy.md) |

When the same content appears in two docs, one of them should be a link.

## Versioning

GPC is pre-1.0 and developed against `main`. Tagged releases will follow
SemVer once the public surface stabilizes. Until then, breaking changes are
allowed on `main` but should be called out in `CHANGELOG.md` (to be created
at the first tagged release).

## Releases

No CI pipeline ships with the repository today. When publishing a release:

1. Update `requirements.txt` only if a dependency change is required.
2. Run the smoke suite locally.
3. Tag the commit (`git tag -a vX.Y.Z -m "..."`) and push the tag.
4. Draft GitHub release notes from the commit log since the previous tag.

## Hosting the Landing Page

`site/` is a fully static page (HTML + CSS + JS + SVG). Any static host
works (GitHub Pages, Netlify, Cloudflare Pages, S3 + CloudFront). No build
step is required.
