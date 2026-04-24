# Quickstart with Three Projects

This walkthrough indexes three local repositories into a single GPC
installation and shows how AI clients then retrieve from the same memory.

The example uses three repositories that belong to one logical product:

| Slug | Role |
|---|---|
| `my-api` | Backend API. |
| `my-workers` | Background workers. |
| `my-web` | Frontend web app. |

Each project keeps its own slug and index while sharing the same local
Postgres, Qdrant, Ollama, Neo4j and MCP infrastructure.

## 1. Install GPC

```bash
./install.sh
gpc doctor
```

`gpc doctor` validates Docker services, Ollama and the embedding model.

## 2. Configure MCP Clients

```bash
gpc install-clients
```

This registers GPC as an MCP server named `gpc` in every supported client
found on the machine. Pass `--clients ...` to limit the scope.

## 3. Initialize the Three Projects

```bash
gpc init ~/Projects/my-api      --slug my-api      --name "My API"
gpc init ~/Projects/my-workers  --slug my-workers  --name "My Workers"
gpc init ~/Projects/my-web      --slug my-web      --name "My Web"
```

`gpc init` registers project identity, installs the auto-index Git hooks and
runs the first full index for each repository.

If those checkouts are parts of one product rather than separate projects, use
one logical project and attach each checkout as a repo:

```bash
gpc project create my-product --name "My Product"
gpc init ~/Projects/my-api      --project my-product --repo my-api
gpc init ~/Projects/my-workers  --project my-product --repo my-workers
gpc init ~/Projects/my-web      --project my-product --repo my-web
```

## 4. Verify Coverage

```bash
gpc-status --project my-api
gpc-status --project my-workers
gpc-status --project my-web
```

Each command should report a non-zero file and chunk count and a recent
successful index run.

## 5. Estimate Token Savings

`gpc token-savings` measures one project per call. Run it once per project
with the same query to compare:

```bash
gpc token-savings "how does login move from the frontend to the database?" --project my-api
gpc token-savings "how does login move from the frontend to the database?" --project my-workers
gpc token-savings "how does login move from the frontend to the database?" --project my-web
```

Cross-project graph reasoning (e.g. *"trace login from the web component to
the worker that writes the audit row"*) currently requires either:

- combining the three retrieval results in the calling AI client, or
- using the optional [Graphify Neo4j projection](graphify.md) to traverse
  edges across repositories.

A native cross-project retrieval mode is on the roadmap.

## 6. Use From an AI Client

Once the projects are indexed and `gpc install-clients` has registered the
server, ask the client to call GPC:

```text
Use the gpc MCP server. Call gpc.context with project=my-web and
query="the login flow".
```

```text
Use gpc.context with cwd=~/Projects/my-api and query="how is the user
validated?".
```

See [mcp-clients.md](mcp-clients.md) for the full tool reference.

## See Also

- [Operations](operations.md) — install, validate and reset.
- [Automation](automation.md) — what the Git hooks installed by `gpc init`
  actually do.
- [Token economy](token-economy.md) — interpreting the savings numbers.
