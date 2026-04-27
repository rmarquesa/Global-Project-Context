# GPC Roadmap

Roadmap vivo do GPC. Cada fase tem objetivo, entregáveis concretos, arquivos
afetados e critério de pronto. Atualize à medida que iniciativas concluam ou
mudem de direção.

## Princípios norteadores

- **MCP read-only no hot path.** Escritas e cargas pesadas ficam em CLI/hooks.
- **Honestidade dos dados.** Confidence tags (`EXTRACTED` / `INFERRED` /
  `AMBIGUOUS`) nunca são ocultadas do consumidor.
- **Namespacing por `project_slug + repo_slug`.** Nenhum tool cruza projeto sem
  filtro explícito.
- **Reconstruível.** Postgres é fonte da verdade; Qdrant e Neo4j são
  read-models descartáveis.

---

## Fase 1 — Grafo estrutural acessível pelo MCP (entregue)

**Entregue em 2026-04-23.** O MCP agora consegue responder perguntas de
topologia (quem toca quem) além das de semântica (o que parece com isso).

- [x] Módulo [gpc/graph_query.py](../gpc/graph_query.py) com
      `graph_neighbors`, `graph_summary`, `graph_path`. Todas scoped por
      `project_slug`, filtram por `min_confidence` e restringem traversal a
      `GRAPHIFY_RELATION | CROSS_REPO_BRIDGE` (plumbing como `HAS_REPO` /
      `OWNS_REPO` não vaza).
- [x] MCP tools `gpc.graph_neighbors`, `gpc.graph_summary`, `gpc.graph_path`
      em [gpc/mcp_server.py](../gpc/mcp_server.py).
- [x] Default `min_confidence="EXTRACTED"` — `INFERRED` (ex.: bridges
      cross-repo) e `AMBIGUOUS` só aparecem com opt-in explícito. Cada aresta
      carrega `confidence`, `confidence_score`, `rule` e `evidence`.
- [x] Smoke test
      [tests/smoke/graph_query_smoke_test.py](../tests/smoke/graph_query_smoke_test.py):
      valida filtro por confidence, filtro por relação, isolamento por
      projeto, e shortestPath com e sem bridges.
- [x] [tests/smoke/mcp_smoke_test.py](../tests/smoke/mcp_smoke_test.py)
      exercita os três tools novos via stdio; hoje lista 12 tools.
- [x] Docs: [docs/mcp-clients.md](mcp-clients.md) com tabela de tools +
      explicação "semantic vs structural", [docs/architecture.md](architecture.md)
      e [README.md](../README.md) atualizados.

Expansões naturais que ficam na Fase 2:
- [x] `gpc.graph_community` — entregue em 2026-04-24.
- [x] `gpc.graph_diff` — entregue em 2026-04-24 junto com a coleta
      longitudinal `gpc_self_metrics` (Fase 3.1).
- [ ] Parâmetro `relations` aceitar regex ou incluir relações GPC-side
      (`OWNS_ENTITY`, `GPC_RELATION`) na camada `graph_neighbors`.

## Fase 1.7 — Observabilidade do MCP (entregue)

**Entregue em 2026-04-24.** Cada chamada ao MCP agora é auditável — responde
à pergunta prática "minhas ferramentas de IA estão de fato usando o GPC?".

- [x] Migration `0006_mcp_call_log.sql` cria `gpc_mcp_calls` (tool, project,
      client, cwd, duration_ms, success, args/result_meta jsonb).
- [x] [gpc/mcp_observability.py](../gpc/mcp_observability.py) define o
      decorator `@log_mcp_call` que envolve cada tool. Nunca levanta: se o
      Postgres estiver indisponível, loga em stderr e libera a resposta.
- [x] Todos os 13 tools decorados. Novo tool `gpc.mcp_usage(window_hours)`
      agrega o log em totais / por tool / por client / por projeto.
- [x] Smoke test [tests/smoke/mcp_observability_smoke_test.py](../tests/smoke/mcp_observability_smoke_test.py)
      valida que tools deixam rastro auditável e se limpa depois.
- [x] Docs: seção "Auditing MCP Usage" em [docs/mcp-clients.md](mcp-clients.md)
      e nota em [docs/token-economy.md](token-economy.md) sobre baseline
      otimista vs economia realista (30–97% dependendo do tipo de pergunta).

### Fase 1.7.1 — Grafana e retenção (entregue)

**Entregue em 2026-04-26.** Observabilidade deixa de ser só auditoria SQL e
vira dashboard operacional.

- [x] Migration `0008_token_savings_samples.sql` cria
      `gpc_token_savings_samples`, preenchida automaticamente por
      `gpc.search`, `gpc.context` e `gpc.estimate_token_savings`.
- [x] `docker-compose.yaml` ganha serviço `grafana` no profile
      `observability`, com datasource Postgres e dashboard versionado em
      `observability/grafana/`.
- [x] [docs/observability.md](observability.md) documenta dashboard,
      retention e queries úteis.
- [x] CLI `gpc maintenance retention --mcp-days 30 --token-days 90`
      remove linhas antigas de `gpc_mcp_calls` e
      `gpc_token_savings_samples`.
- [x] `gpc install-clients` passa a rotular clientes suportados com
      `GPC_MCP_CLIENT=<client>`.

## Fase 1.10 — Project delete (entregue)

**Entregue em 2026-04-24.** Simétrico ao rename: apagar um projeto sem
deixar vestígio em nenhuma das três camadas, nem no filesystem.

- [x] [gpc/project_delete.py](../gpc/project_delete.py):
      `delete_project(slug, remove_hooks=True, remove_local_files=True)`
      faz count + cascade delete em Postgres (dentro de transação),
      delete filtrado em Qdrant, `DETACH DELETE` em todos os labels
      slug-bearing do Neo4j, e remove por default os hooks git
      gerenciados pelo GPC + `.gpc.yaml` + `.gpc/` em cada root
      registrado. Hooks customizados são preservados e reportados em
      `hooks_skipped`. Canoniza paths (`Path.resolve`) antes de
      iterar pra evitar tocar o mesmo diretório duas vezes por
      symlinks tipo `/var` → `/private/var` no macOS.
- [x] CLI `gpc project delete <slug> --yes [--keep-hooks] [--keep-local-files] [--json]`.
- [x] Smoke test [tests/smoke/project_delete_smoke_test.py](../tests/smoke/project_delete_smoke_test.py)
      cria projeto sintético com rows em todas as tabelas, ponto
      Qdrant, projeção Graphify em Neo4j e filesystem com hook
      gerenciado + hook custom + `.gpc/` + `.gpc.yaml`. Valida que
      tudo foi apagado exceto o hook custom e que uma segunda
      invocação levanta `ProjectDeleteError`.
- [x] Aplicado contra `l3-games` no ambiente ao vivo (3814 files,
      9371 chunks, 9125 pontos Qdrant, 3 hooks gerenciados, 1
      `.gpc.yaml`).

## Fase 1.9 — Project rename (entregue)

**Entregue em 2026-04-24.** Corrigir slug de projeto (ex.: placeholder que
vazou em `gpc init`) sem precisar mexer em SQL, Qdrant, Neo4j na mão.

- [x] [gpc/project_rename.py](../gpc/project_rename.py): `rename_project(old, new, new_name=None, rename_default_repo=True)` atualiza Postgres (transação: `gpc_projects`, `gpc_repos` com mesmo slug do projeto, `gpc_self_metrics`, `gpc_project_aliases`), Qdrant (`project_slug`/`project_name` nos payloads), e Neo4j (`GPCProject.slug`, `GraphifyProject.slug`, `project_slug` em repos/entities/nodes, mais `id` dos GraphifyNodes/GraphifyRepos que começam com o slug antigo). Slug antigo vira alias histórico.
- [x] CLI `gpc project rename <old> <new> [--new-name N] [--keep-default-repo] --yes [--json]`.
- [x] Smoke test [tests/smoke/project_rename_smoke_test.py](../tests/smoke/project_rename_smoke_test.py) cobre Postgres + Qdrant + Neo4j.
- [x] Aplicado contra `project-slug` → `l3games` no ambiente ao vivo
      (1 projeto, 1 repo, 144 payloads Qdrant reetiquetados).

## Fase 3.1 — Coleta longitudinal + graph_diff (entregue)

**Entregue em 2026-04-24.** Começa a coletar os sinais longitudinais que
a Fase 3 (auto-research) precisa. Nenhum detector ainda — só coleta e
diff. Detectores de drift herdam disso depois.

- [x] Migration `0007_gpc_self_metrics.sql` — uma linha por run, colunas
      numéricas por campo (size/honesty/topology/freshness) + `god_nodes_top10`
      e `metadata` jsonb para extensibilidade sem nova migration.
- [x] [gpc/self_metrics.py](../gpc/self_metrics.py) — `collect_metrics()`
      junta contagens de Postgres com counts de Neo4j (tolera Neo4j
      indisponível — campos ficam null, row é escrita assim mesmo).
      Exposto também como `list_snapshots`, `fetch_snapshot`, `fetch_pair`.
- [x] Hook no fim de `index_project_path` e `build_bridges` — todo write
      grava snapshot, `source` identifica o trigger.
- [x] CLI `gpc metrics collect --project X [--json]` e `gpc metrics list
      [--project X]`.
- [x] `gpc.graph_diff` — diff entre dois snapshots. Retorna
      `numeric_deltas`, `god_nodes_diff` (entered/exited/stable) e
      `confidence_shift` (antes/depois em % + delta em pp).
- [x] `gpc.self_metrics` — lista ou coleta via MCP (opt-in via
      `collect=true`).
- [x] Smoke test [tests/smoke/self_metrics_smoke_test.py](../tests/smoke/self_metrics_smoke_test.py)
      valida collect + diff.
- [x] Docs: architecture.md (5 superfícies agora), mcp-clients.md tabela
      expandida para 17 tools, README atualizado.

Próximos passos naturais em cima disto (continuam Fase 3):
- [x] Detector de drift — entregue em 2026-04-26 com migration
      `0009_drift_signals.sql`, módulo [gpc/drift.py](../gpc/drift.py),
      CLI `gpc metrics drift/signals` e MCP `gpc.drift_signals`.
- [ ] Job cron semanal que roda `gpc metrics collect` para cada projeto
      (já que hooks só disparam quando há write; projetos parados
      ficariam sem snapshots novos).

## Fase 1.8 — Graph quality improvements (entregue)

**Entregue em 2026-04-24.** Tornar o grafo e o retrieval mais úteis para
agentes sem mexer na premissa boa: MCP read-only, confidence explícita,
escritas via CLI/hooks.

- [x] `graph_summary` separa `god_nodes` (central) de `utility_hubs`
      (genéricos). No alugafacil, `fetch()` sai do topo de centrais e vira
      utility; `passport.js`/`resolveSecret()`/`crypto.js` ficam como hubs
      reais.
- [x] Novo tool `gpc.graph_community(project, community_id)` expõe membros,
      repos envolvidos e bridges externos de uma community. Permite
      navegação após `graph_summary`.
- [x] Regra `content_hash` em `build_bridges` via join com
      `gpc_files.content_hash`. Roda primeiro (strongest signal) e as
      regras posteriores skip pares já cobertos.
- [x] [gpc/entity_extractor.py](../gpc/entity_extractor.py): MVP que
      popula `gpc_entities` (type=file) + `gpc_relations` (type=imports,
      INFERRED 0.75) a cada `index_project_path`. No alugafacil: 567
      entities + 464 imports (same-repo; cross-repo não aparece porque
      workers Cloudflare compartilham código por cópia, não import).
- [x] [gpc/graph.py](../gpc/graph.py): `GPCEntity` agora fica ligado a
      `(:GPCRepo)-[:OWNS_ENTITY]->(:GPCEntity)` também, não só ao projeto.
- [x] Hybrid retrieval em `gpc.context(include_graph=true)`: anexa footer
      com vizinhos Graphify de cada chunk, com `confidence` tagged.
      Default `graph_min_confidence="EXTRACTED"`; INFERRED é opt-in.
- [x] Smoke test
      [tests/smoke/graph_quality_smoke_test.py](../tests/smoke/graph_quality_smoke_test.py)
      cobrindo classificação de god nodes, `graph_community`, entity
      extractor em projeto temporário.

## Fase 1 (original) — Grafo estrutural acessível pelo MCP (opt-in)

**Objetivo.** Dar ao modelo acesso a perguntas estruturais que o retrieval
semântico não resolve: "quem depende de X", "caminho mais curto entre A e B",
"god nodes deste projeto". Sem borrar a promessa atual do `gpc.search` /
`gpc.context`.

### 1.1 `gpc.graph_neighbors`

Retorna vizinhos de um nó no grafo Graphify, filtrados por relação e
confidence. Opt-in — o cliente pede explicitamente.

- Assinatura: `(project, node, depth=2, relations=[…], min_confidence="EXTRACTED", cwd=None)`
- Default: só `EXTRACTED`. `INFERRED` exige opt-in via `min_confidence`.
- Sempre devolve `confidence` e `confidence_score` no payload.
- Filtro obrigatório por `project_slug` para não vazar entre projetos.
- Arquivos: [gpc/mcp_server.py](../gpc/mcp_server.py) (novo tool), nova camada
  `gpc/graph_query.py` usando `neo4j_driver()` de
  [gpc/graph.py](../gpc/graph.py#L34).

### 1.2 `gpc.graph_summary`

Retorna god nodes, communities e cohesion de um projeto — o essencial do
`GRAPH_REPORT.md` em forma estruturada.

- Assinatura: `(project, top_k_gods=10, include_cohesion=True, cwd=None)`
- Fonte: Neo4j (se projeção Graphify existir) com fallback ao
  `graphify-out/GRAPH_REPORT.md` do repo ativo.
- Arquivos: mesmo trio acima; reaproveitar `gpc/token_economy.py` para
  estimativa de custo.

### 1.3 `gpc.graph_path`

Shortest path entre dois símbolos (labels ou IDs).

- Assinatura: `(project, a, b, max_hops=6, min_confidence="EXTRACTED", cwd=None)`
- Retorna a sequência de nós + arestas com relação e confidence por hop.
- Arquivos: mesmo trio.

### Prontidão da fase

- [ ] Os três tools aparecem em `gpc install-clients` e em
  [AGENTS.md](../AGENTS.md).
- [ ] Smoke test dedicado em `tests/smoke/graph_query_smoke_test.py` cobrindo:
  projeto sem projeção (fallback), projeto com projeção (caminho feliz),
  filtro por slug (scope isolation).
- [ ] [docs/mcp-clients.md](mcp-clients.md) lista os três novos tools.
- [ ] Decisão registrada em [docs/architecture.md](architecture.md) sobre por
  que os tools são opt-in e como `min_confidence` protege contra
  alucinação arquitetural.

---

## Fase 1.6 — Modelo projeto+repo e reset controlado (entregue)

**Entregue em 2026-04-23.** Repositórios agora são cidadãos de primeira classe
do GPC, pareados com a projeção Graphify no Neo4j, e há caminho explícito para
zerar infraestrutura e recomeçar.

- [x] Migration `migrations/0004_gpc_repos.sql` cria `gpc_repos` e adiciona
      `repo_id` em `gpc_files` / `gpc_chunks`, com backfill automático.
- [x] Registry: `ensure_project`, `register_repo`, `list_repos`,
      `resolve_repo`, `consolidate_projects` em
      [gpc/registry.py](../gpc/registry.py).
- [x] CLI: `gpc project create|list|consolidate`, `gpc repo add|list|remove`,
      `gpc init --project <slug> --repo <slug>` para anexar um diretório como
      repo de um projeto existente.
- [x] Indexer preenche `repo_id` em files/chunks e `repo_slug` no payload
      Qdrant (usado pelos filtros de `gpc.search` e `gpc.context`).
- [x] Projeção Neo4j agora inclui `(:GPCProject)-[:OWNS_REPO]->(:GPCRepo)`
      em [gpc/graph.py](../gpc/graph.py).
- [x] MCP ganha `gpc.list_repos`, `gpc.resolve_repo`, e parâmetro `repo` em
      `gpc.search` / `gpc.context`. `gpc.list_projects` já retorna os repos
      de cada projeto para evitar round-trip extra.
- [x] Reset controlado: `gpc graph-reset --yes [--project] [--rebuild]`
      só zera Neo4j; `gpc reset --yes` é nuclear (Postgres drop + migrate,
      Neo4j wipe, Qdrant recreate).
- [x] Smokes: `tests/smoke/repo_registry_smoke_test.py` e
      `tests/smoke/graph_reset_smoke_test.py`.
- [x] Documentação: `docs/architecture.md` (seção "Project + repo model"),
      `AGENTS.md` (seções "GPC project + repo model" e atualizações em
      "GPC MCP").

Gap conhecido identificado no processo:
- [ ] `gpc_entities` e `gpc_relations` estão vazios no ambiente atual porque
      o indexer ainda não as popula. O único consumidor dessas tabelas é
      `project_graph_to_neo4j`, que hoje projeta sempre 0 entities. Criar
      extrator de entities a partir de chunks é um item da Fase 2.

## Fase 1.5 — Bridging cross-repo (entregue)

**Entregue em 2026-04-23.** Graphify roda isolado em cada repo; as pontes entre
repos do mesmo projeto agora são criadas no Neo4j após a projeção, sem exigir
monorepo.

- [x] Módulo [gpc/cross_repo.py](../gpc/cross_repo.py) com 3 regras tiered:
      `same_source_file` (INFERRED 0.90), `same_code_symbol` (INFERRED 0.75),
      `same_generic_symbol` (AMBIGUOUS 0.30, opt-in).
- [x] Comando `gpc graph-bridge [--project <slug>] [--rule R] [--include-ambiguous] [--clear]`.
- [x] Hook `examples/hooks/graphify-neo4j-post-commit.sh` chama bridging
      automaticamente via `GPC_GRAPHIFY_BRIDGE_AFTER=1` (default).
- [x] Smoke test [tests/smoke/cross_repo_bridge_test.py](../tests/smoke/cross_repo_bridge_test.py)
      cobrindo: regra default, opt-in AMBIGUOUS, idempotência, isolamento por
      projeto.
- [x] Doc em [docs/architecture.md#cross-repo-bridging](architecture.md)
      e [docs/graphify.md](graphify.md).
- [x] Rodado contra `alugafacil`: 235 arestas escritas — 213
      `same_source_file` + 22 `same_code_symbol`.

Próximas evoluções naturais (ficam na Fase 2):
- [ ] Regra `package_import` — parse de `package.json` / `import` statements
      para upgrade de INFERRED → EXTRACTED em casos com qualificação de pacote.
- [x] Regra `content_hash` — entregue em 2026-04-24 (INFERRED 0.95). No
      alugafacil ainda não captura sinal porque cada worker tem sua própria
      implementação de `crypto.js`; fica útil quando há vendoring bit-a-bit.

## Fase 2 — Multi-projeto (Cloudflare backend + frontend + database)

**Objetivo.** Validar GPC com 3 projetos reais e descobrir gaps antes de
escalar. Backend Cloudflare com muitos workers é o caso mais exigente porque
mistura muitos repos pequenos sob um `project_slug` comum.

### 2.1 Checklist por projeto onboardado

Rodar, em ordem, para cada novo projeto antes de declarar "indexado":

- [ ] `gpc init . --slug <slug> --name "<Name>"`
- [ ] `gpc-index` completo + `gpc.index_status` verde
- [ ] `graphify update .` + confirmar `GRAPH_REPORT.md` não-vazio
- [ ] Post-commit hook Graphify → Neo4j instalado
  ([examples/hooks/graphify-neo4j-post-commit.sh](../examples/hooks/graphify-neo4j-post-commit.sh))
- [ ] `gpc.search` retorna chunks coerentes para 3 perguntas representativas
- [ ] `gpc.graph_summary` (Fase 1) lista god nodes que fazem sentido para um
  humano do projeto

### 2.2 Consolidação Cloudflare workers

Cloudflare backend com N workers é o teste de estresse para consolidação
cross-repo ([docs/graphify.md:50-52](graphify.md#L50-L52)).

- [ ] Escolher `project_slug` comum (ex. `cf-backend`) e `repo_slug` por worker.
- [ ] Validar que nós Graphify usam IDs estáveis
  `<project_slug>:<repo_slug>:<graphify_node_id>`.
- [ ] Rodar `gpc.graph_path` entre dois workers que compartilham um contrato
  — resultado esperado: caminho curto passando pelo símbolo compartilhado.
- [ ] Medir freshness lag: tempo entre commit num worker e Neo4j refletindo.

### 2.3 Auditoria cross-projeto

Depois de indexar backend/frontend/database, rodar uma passagem de análise
com o próprio GPC (dogfooding):

- [ ] `gpc.graph_summary` em cada projeto — registrar god nodes e communities.
- [ ] Comparar convenções: naming, estrutura de config, modelo de embeddings
  (todos devem usar `nomic-embed-text:latest` —
  [AGENTS.md:19](../AGENTS.md#L19)).
- [ ] Listar gaps/inconsistências encontradas num documento
  `docs/audit-<date>.md`.
- [ ] Criar issues para cada gap classificado como crítico.

### Prontidão da fase

- [ ] Três projetos indexados, com projeção Neo4j ativa.
- [ ] Relatório de auditoria com recomendações priorizadas.
- [ ] Zero vazamento de contexto entre projetos validado por smoke test
  dedicado.

---

## Fase 3 — Auto-research e melhoria contínua

**Objetivo.** Transformar GPC num sistema que observa a si mesmo e propõe
melhorias. Nada de auto-merge; o objetivo é **gerar sinais e propostas**
revisáveis por humanos, não executar mudanças sozinho.

### 3.1 Métricas longitudinais

Coletar sem interpretar ainda. Uma tabela Postgres `gpc_self_metrics` com
entrada por run:

- Contagem de nós / arestas / communities por projeto.
- Distribuição de confidence (% EXTRACTED vs INFERRED vs AMBIGUOUS).
- God nodes top-10 (hash da lista) — detectar mudança de "núcleo".
- Weakly-connected node count (proxy de gaps de documentação).
- Token reduction do benchmark graphify.
- Freshness lag: `max(age)` entre Postgres `gpc_files.updated_at` e nó
  Neo4j correspondente.

Arquivo novo: `gpc/self_metrics.py`. Migração nova em
[migrations/](../migrations/).

### 3.2 Detecção de drift

**Entregue em 2026-04-26.** O detector inicial é rule-based, grava sinais em
`gpc_drift_signals`, e nunca executa mudanças.

Após cada `gpc-index` ou `graphify update`, comparar com o snapshot anterior
e emitir sinais — nunca ações:

- [x] Top-3 god nodes mudou → possível refactor mudou o núcleo.
- [x] Community count subiu >50% e pelo menos 3 communities.
- [x] % INFERRED subiu >10 pp → extração perdeu sinais estruturais.
- [x] Weakly-connected nodes subiu >50% e pelo menos 10 nodes.

Implementação: job offline em [gpc/drift.py](../gpc/drift.py), grava
`gpc_drift_signals`. Exposto via `gpc metrics drift`, `gpc metrics signals`
e `gpc.drift_signals(project)` (tool MCP opt-in).

### 3.3 Loop de auto-proposição (humano-no-loop)

Rodar periodicamente (cron ou `/loop`) um agente que:

1. Lê `gpc_drift_signals` e `gpc_self_metrics`.
2. Consulta o grafo para contextualizar o sinal.
3. Produz uma proposta em markdown para
   `docs/proposals/YYYY-MM-DD-<slug>.md` com:
   - Sinal observado e evidência (nós, confidence).
   - Hipótese causal.
   - Mudança sugerida (código, docs ou indexação).
   - Risco e como reverter.
4. Nunca abre PR automaticamente. Nunca modifica código. Só escreve a
   proposta.

Arquivo novo: `scripts/auto_research.py`. Agendamento via
`graphify schedule` ou cron externo.

### 3.4 Portfólio de testes derivados do grafo

Gerar testes smoke a partir da topologia:

- Para cada god node com `calls` para um módulo externo, existe um smoke test
  que exercita o caminho?
- Para cada `rationale_for` marcado EXTRACTED, existe implementação
  correspondente?

Discrepâncias viram entradas no relatório de drift.

### 3.5 Sandbox de experimentação

Pasta `experiments/` (gitignored por default) onde o loop de auto-research
pode escrever protótipos isolados sem tocar em `gpc/`. Qualquer promoção
para `gpc/` passa por PR humano com `tests/smoke/` verde.

### Prontidão da fase

- [ ] `gpc_self_metrics` popula após cada run por pelo menos 2 semanas.
- [ ] Primeiro `docs/proposals/` gerado automaticamente.
- [ ] Pelo menos uma proposta promovida a PR + merge após revisão humana,
  provando o loop fim-a-fim.

---

## Fora do escopo declarado

Coisas que já foram consideradas e conscientemente adiadas. Não são "TODO",
são "não agora":

- MCP chamando Neo4j no hot path de `gpc.search` / `gpc.context` — mantém a
  promessa de previsibilidade e read-model descartável.
- Treino de modelo próprio ([architecture_rationale](architecture.md#L23)
  explica).
- Auto-merge de propostas da Fase 3.
- Exposição HTTP pública — MCP permanece local por default
  ([SECURITY.md](../SECURITY.md)).

---

## Como atualizar este documento

- Cada item concluído vira um `[x]` e ganha link para o commit / PR.
- Cada item abandonado migra para "Fora do escopo" com uma linha explicando
  por quê.
- Novas fases entram numeradas e sempre com: objetivo, entregáveis,
  arquivos, prontidão.
