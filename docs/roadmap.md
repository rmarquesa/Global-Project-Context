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
- [ ] `gpc.graph_diff` — diff estrutural entre duas projeções (útil para
      detectar drift após cada run).
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

## Fase 1.7 gaps naturais (próxima iteração de observabilidade):
- [ ] Hook em clients oficiais para preencher `GPC_MCP_CLIENT` (hoje só
      clientes que setam variáveis próprias — Claude Code, Codex, Copilot —
      são identificados automaticamente).
- [ ] Job de retenção (`DELETE FROM gpc_mcp_calls WHERE called_at < now() -
      interval '30 days'`) documentado como cron exemplo.

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

Após cada `gpc-index` ou `graphify update`, comparar com o snapshot anterior
e emitir sinais — nunca ações:

- God node desapareceu do top-10 → possível refactor mudou o núcleo.
- Community dobrou de tamanho → possível erosão de fronteiras.
- % INFERRED subiu >10 pp → extração perdeu sinais estruturais.
- Novo nó com grau >15 aparecendo sem histórico → hub criado recentemente,
  merece atenção.

Implementação: job offline, grava `gpc_drift_signals`. Exposto via
`gpc.drift_signals(project)` (tool MCP opt-in).

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
