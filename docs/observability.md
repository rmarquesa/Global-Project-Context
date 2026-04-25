# Observability

GPC records MCP activity in Postgres and ships a ready-to-use Grafana dashboard
for token economy and MCP server health.

## Start Grafana

Grafana is included in `docker-compose.yaml` behind the `observability`
profile:

```bash
docker compose --profile observability up -d grafana
```

Open `http://localhost:3000`.

Default credentials:

```text
admin / gpcgrafanapass
```

Override them with:

```env
GPC_GRAFANA_ADMIN_USER=admin
GPC_GRAFANA_ADMIN_PASSWORD=change-me
GPC_GRAFANA_PORT=3000
```

## Provisioned Dashboard

The Compose service mounts:

- `observability/grafana/provisioning/datasources/postgres.yml`
- `observability/grafana/provisioning/dashboards/dashboards.yml`
- `observability/grafana/dashboards/gpc-token-economy.json`

Grafana creates the `GPC Postgres` datasource automatically and loads the
`GPC Token Economy` dashboard into the `GPC` folder.

The dashboard shows tokens saved over time, average savings percentage, MCP
calls, error rate, calls by tool, saved tokens by repository, recent samples,
and slow or failed MCP calls.

## Data Model

Every MCP tool invocation is logged to `gpc_mcp_calls` by `@log_mcp_call` in
`gpc/mcp_observability.py`.

Token economy samples are additionally written to `gpc_token_savings_samples`
when one of these tools returns successfully:

- `gpc.search`
- `gpc.context`
- `gpc.estimate_token_savings`

The sample stores counts only: project, optional repo, query, indexed tokens,
retrieved tokens, saved tokens, savings percentage, returned character count
and result count. It does not store retrieved source code or full context text.

The telemetry path is best-effort. If Postgres is unavailable, or the migration
has not been applied yet, the MCP call still returns normally and the logger
prints a warning to stderr.

## Apply Migration

Existing installations need the new table:

```bash
gpc migrate up
```

Samples are not retroactive. The dashboard starts filling after new MCP calls
hit `gpc.search`, `gpc.context` or `gpc.estimate_token_savings`.

## Useful SQL

Recent samples:

```sql
select created_at, tool, project_slug, repo_slug, query,
       retrieved_tokens, saved_tokens, savings_percent
from gpc_token_savings_samples
order by created_at desc
limit 20;
```

Savings by project in the last day:

```sql
select project_slug,
       count(*) as samples,
       sum(saved_tokens) as saved_tokens,
       round(avg(savings_percent), 2) as avg_savings_percent
from gpc_token_savings_samples
where created_at > now() - interval '24 hours'
group by project_slug
order by saved_tokens desc;
```

MCP usage by tool:

```sql
select tool,
       count(*) as calls,
       count(*) filter (where not success) as errors,
       avg(duration_ms)::int as avg_ms
from gpc_mcp_calls
where called_at > now() - interval '24 hours'
group by tool
order by calls desc;
```

## OTEL

The current implementation persists observability in Postgres and visualizes it
with Grafana. OpenTelemetry can be added on top of the same measurement points
later, exporting counters and spans to an OTEL collector without replacing the
Postgres audit trail.
