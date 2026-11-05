# GPC Roadmap

Roadmap vivo do GPC. Cada fase tem objetivo, entregáveis concretos, arquivos
afetados e critério de pronto. Atualize à medida que iniciativas concluam ou
mudem de direção.

O próximo ciclo local-first está detalhado em
[Context-engine evolution (2026-11-04)](plans/2026-11-04-gpc-context-engine-evolution.md):
confiabilidade primeiro, depois atualização contínua, recuperação orientada à
tarefa, memória com evidências e contexto de engenharia. Confiabilidade, watcher
opcional e recuperação híbrida passaram pela validação local. O incremento atual
implementa memória de decisões com aprovação explícita; conectores externos e
reranking aprendido continuam planejados. Cada entrega deve atualizar também o
site do produto, sem confundir implementação local com publicação ou benchmark.

## Princípios norteadores

- **MCP read-only no hot path.** Escritas e cargas pesadas ficam em CLI/hooks.
- **Honestidade dos dados.** Confidence tags (`EXTRACTED` / `INFERRED` /
  `AMBIGUOUS`) nunca são ocultadas do consumidor.
- **Namespacing por `project_slug + repo_slug`.** Nenhum tool cruza projeto sem
  filtro explícito.
- **Reconstruível.** Postgres é fonte da verdade; Qdrant e Neo4j são
  read-models descartáveis.

---

## Fase 2.1 — Product hardening e operator workflow (entregue)

**Entregue em 2026-05-07.** O GPC ganhou comandos operacionais para verificar,
avaliar, empacotar e auditar contexto sem quebrar a regra central: MCP continua
read-only; escritas e diagnósticos ficam na CLI.

- [x] `gpc graph-sync` sincroniza `graphify-out/graph.json` para Neo4j por
      `project_slug + repo_slug`, com smoke de self-visibility.
- [x] `gpc verify` consolida registry, índice, Graphify, staleness e serviços.
- [x] `gpc stale` e `gpc-status --staleness` detectam divergência entre Git,
      Postgres e Graphify.
- [x] `gpc eval-search` executa fixture de qualidade de busca com recall@k.
- [x] `gpc context-pack` gera Markdown citado e limitado por orçamento.
- [x] `gpc health-report` resume sinais de operação para humanos/CI local.
- [x] `gpc mcp-usage` expõe auditoria redigida de chamadas MCP.
- [x] `gpc maintenance doctor` adiciona diagnóstico dry-run sem mutação.
- [x] Testes unitários adicionais cobrem parser CLI, chunking/discovery do
      indexer, payloads MCP, redaction, manutenção, context packs e search eval.

## Fase 1 — Grafo estrutural acessível pelo MCP (entregue)

**Entregue em 2026-04-23.** O MCP agora consegue responder perguntas de
topologia (quem toca quem) além das de semântica (o que parece com isso).

- [x] Módulo [gpc/graph_query.py](../gpc/graph_query.py) com
      `graph_neighbors`, `graph_summary`, `graph_path`. Todas scoped por
      `project_slug`, filtram por `min_confidence` e restringem traversal a
      `GRAPHIFY_RELATION | CROSS_REPO_BRIDGE` (plumbing como `HAS_REPO` /
      `OWNS_REPO` não vaza).
- [x] MCP tools `gpc_graph_neighbors`, `gpc_graph_summary`, `gpc_graph_path`
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
- [x] `gpc_graph_community` — entregue em 2026-04-24.
- [x] `gpc_graph_diff` — entregue em 2026-04-24 junto com a coleta
      longitudinal `gpc_self_metrics` (Fase 3.1).
- [x] `graph_neighbors.relations` mantém lista exata e inclui, sob solicitação
      explícita, relações GPC-side (`OWNS_ENTITY`, `GPC_RELATION`), sem expor
      plumbing (`HAS_REPO`, `HAS_GRAPH_NODE`, `OWNS_REPO`). Nós `GPCEntity` e
      confidence explícita são documentados e cobertos por smoke test.

## Fase 1.7 — Observabilidade do MCP (entregue)

**Entregue em 2026-04-24.** Cada chamada ao MCP agora é auditável — responde
à pergunta prática "minhas ferramentas de IA estão de fato usando o GPC?".

- [x] Migration `0006_mcp_call_log.sql` cria `gpc_mcp_calls` (tool, project,
      client, cwd, duration_ms, success, args/result_meta jsonb).
- [x] [gpc/mcp_observability.py](../gpc/mcp_observability.py) define o
      decorator `@log_mcp_call` que envolve cada tool. Nunca levanta: se o
      Postgres estiver indisponível, loga em stderr e libera a resposta.
- [x] Todos os 13 tools decorados. Novo tool `gpc_mcp_usage(window_hours)`
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
      `gpc_search`, `gpc_context` e `gpc_estimate_token_savings`.
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
- [x] `gpc_graph_diff` — diff entre dois snapshots. Retorna
      `numeric_deltas`, `god_nodes_diff` (entered/exited/stable) e
      `confidence_shift` (antes/depois em % + delta em pp).
- [x] `gpc_self_metrics` — lista ou coleta via MCP (opt-in via
      `collect=true`).
- [x] Smoke test [tests/smoke/self_metrics_smoke_test.py](../tests/smoke/self_metrics_smoke_test.py)
      valida collect + diff.
- [x] Docs: architecture.md (5 superfícies agora), mcp-clients.md tabela
      expandida para 17 tools, README atualizado.

Próximos passos naturais em cima disto (continuam Fase 3):
- [x] Detector de drift — entregue em 2026-04-26 com migration
      `0009_drift_signals.sql`, módulo [gpc/drift.py](../gpc/drift.py),
      CLI `gpc metrics drift/signals` e MCP `gpc_drift_signals`.
- [x] Job semanal disponível via `gpc metrics collect-all` e
      [scripts/collect_metrics_all.sh](../scripts/collect_metrics_all.sh).
      A rotina percorre todos os projetos registrados, continua após falhas
      individuais e retorna código não-zero se algum projeto falhar; a
      instalação em cron está documentada em
      [docs/operations.md](operations.md).

## Fase 1.8 — Graph quality improvements (entregue)

**Entregue em 2026-04-24.** Tornar o grafo e o retrieval mais úteis para
agentes sem mexer na premissa boa: MCP read-only, confidence explícita,
escritas via CLI/hooks.

- [x] `graph_summary` separa `god_nodes` (central) de `utility_hubs`
      (genéricos). No alugafacil, `fetch()` sai do topo de centrais e vira
      utility; `passport.js`/`resolveSecret()`/`crypto.js` ficam como hubs
      reais.
- [x] Novo tool `gpc_graph_community(project, community_id)` expõe membros,
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
- [x] Hybrid retrieval em `gpc_context(include_graph=true)`: anexa footer
      com vizinhos Graphify de cada chunk, com `confidence` tagged.
      Default `graph_min_confidence="EXTRACTED"`; INFERRED é opt-in.
- [x] Smoke test
      [tests/smoke/graph_quality_smoke_test.py](../tests/smoke/graph_quality_smoke_test.py)
      cobrindo classificação de god nodes, `graph_community`, entity
      extractor em projeto temporário.

## Fase 1 (original) — Grafo estrutural acessível pelo MCP (opt-in)

**Objetivo.** Dar ao modelo acesso a perguntas estruturais que o retrieval
semântico não resolve: "quem depende de X", "caminho mais curto entre A e B",
"god nodes deste projeto". Sem borrar a promessa atual do `gpc_search` /
`gpc_context`.

### 1.1 `gpc_graph_neighbors`

Retorna vizinhos de um nó no grafo Graphify, filtrados por relação e
confidence. Opt-in — o cliente pede explicitamente.

- Assinatura: `(project, node, depth=2, relations=[…], min_confidence="EXTRACTED", cwd=None)`
- Default: só `EXTRACTED`. `INFERRED` exige opt-in via `min_confidence`.
- Sempre devolve `confidence` e `confidence_score` no payload.
- Filtro obrigatório por `project_slug` para não vazar entre projetos.
- Arquivos: [gpc/mcp_server.py](../gpc/mcp_server.py) (novo tool), nova camada
  `gpc/graph_query.py` usando `neo4j_driver()` de
  [gpc/graph.py](../gpc/graph.py#L34).

### 1.2 `gpc_graph_summary`

Retorna god nodes, communities e cohesion de um projeto — o essencial do
`GRAPH_REPORT.md` em forma estruturada.

- Assinatura: `(project, top_k_gods=10, include_cohesion=True, cwd=None)`
- Fonte: Neo4j (se projeção Graphify existir) com fallback ao
  `graphify-out/GRAPH_REPORT.md` do repo ativo.
- Arquivos: mesmo trio acima; reaproveitar `gpc/token_economy.py` para
  estimativa de custo.

### 1.3 `gpc_graph_path`

Shortest path entre dois símbolos (labels ou IDs).

- Assinatura: `(project, a, b, max_hops=6, min_confidence="EXTRACTED", cwd=None)`
- Retorna a sequência de nós + arestas com relação e confidence por hop.
- Arquivos: mesmo trio.

### Prontidão da fase

- [x] Os três tools estão registrados no servidor MCP e documentados em
  [docs/mcp-clients.md](mcp-clients.md) e
  [docs/architecture.md](architecture.md). O repositório não contém
  `AGENTS.md`; portanto, não há uma cópia adicional a manter sincronizada.
- [x] Smoke test dedicado em
  [tests/smoke/graph_query_smoke_test.py](../tests/smoke/graph_query_smoke_test.py)
  cobre filtro por confidence, relações, isolamento por projeto e traversal.
- [x] A decisão sobre opt-in e `min_confidence` está registrada em
  [docs/architecture.md](architecture.md): o default expõe apenas fatos
  `EXTRACTED`, enquanto inferências exigem opt-in explícito.

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
      Qdrant (usado pelos filtros de `gpc_search` e `gpc_context`).
- [x] Projeção Neo4j agora inclui `(:GPCProject)-[:OWNS_REPO]->(:GPCRepo)`
      em [gpc/graph.py](../gpc/graph.py).
- [x] MCP ganha `gpc_list_repos`, `gpc_resolve_repo`, e parâmetro `repo` em
      `gpc_search` / `gpc_context`. `gpc_list_projects` já retorna os repos
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
- [x] `gpc_entities` e `gpc_relations` agora são preenchidos pelo extrator
      acionado ao final do indexador. O módulo
      [gpc/entity_extractor.py](../gpc/entity_extractor.py) cria entidades de
      arquivo e relações de import, com smoke test em
      [tests/smoke/graph_quality_smoke_test.py](../tests/smoke/graph_quality_smoke_test.py).

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
- [x] Regra `package_import` — nós Graphify com identidade explícita de
      pacote/módulo são pareados entre repos e promovidos de INFERRED →
      EXTRACTED somente quando a qualificação é clara.
- [x] Regra `content_hash` — entregue em 2026-04-24 (INFERRED 0.95). No
      alugafacil ainda não captura sinal porque cada worker tem sua própria
      implementação de `crypto.js`; fica útil quando há vendoring bit-a-bit.

## Fase 2 — Multi-projeto (onboarding de projetos reais adicionais)

**Objetivo.** Validar GPC com 3 projetos reais e descobrir gaps antes de
escalar. Não teremos um backend Cloudflare próprio para isso — o caso mais
exigente (muitos repos pequenos sob um `project_slug` comum, como visto
historicamente em [docs/audit-2026-04-26-alugafacil.md](audit-2026-04-26-alugafacil.md))
segue como referência de cenário difícil, mas os projetos reais que vão
compor a Fase 2 ainda precisam ser escolhidos entre os projetos disponíveis
no momento do onboarding — não há mais um alvo fixo de stack.

### 2.1 Checklist por projeto onboardado

O checkout do próprio GPC foi validado em 2026-09-17 como
`gpc-local-verify`: Graphify produziu 1.170 nós, 2.947 relações e 49
communities; a projeção Neo4j foi sincronizada e `gpc verify --live` terminou
com 10/10 checks passando. Os itens abaixo continuam pendentes apenas para os
dois projetos adicionais necessários à meta da fase.

Rodar, em ordem, para cada novo projeto antes de declarar "indexado":

- [ ] `gpc init . --slug <slug> --name "<Name>"`
- [ ] `gpc-index` completo + `gpc_index_status` verde
- [ ] `graphify update .` + confirmar `GRAPH_REPORT.md` não-vazio
- [ ] Post-commit hook Graphify → Neo4j instalado
  ([examples/hooks/graphify-neo4j-post-commit.sh](../examples/hooks/graphify-neo4j-post-commit.sh))
- [ ] `gpc_search` retorna chunks coerentes para 3 perguntas representativas
- [ ] `gpc_graph_summary` (Fase 1) lista god nodes que fazem sentido para um
  humano do projeto

### 2.2 Consolidação multi-repo

Qualquer projeto real que agrupe muitos repositórios pequenos sob um único
`project_slug` (o antigo caso Cloudflare é só um exemplo desse padrão, não
mais um alvo específico) serve de teste de estresse para consolidação
cross-repo ([docs/graphify.md:50-52](graphify.md#L50-L52)).

O mecanismo de consolidação foi validado sinteticamente em 2026-09-17 via
`tests/test_multi_repo_graph_path.py` e `tests/test_graphify_freshness.py`
(dois repos sintéticos `worker-a`/`worker-b` sob um mesmo `project_slug`,
contrato compartilhado, bridge automática e medição de lag). Falta apenas
aplicar o mesmo processo a um projeto real com múltiplos repos — qual
projeto ainda está em aberto, já que não há mais um backend Cloudflare
específico reservado para isso.

- [ ] Escolher, entre os projetos reais disponíveis, um que agrupe múltiplos
  repositórios sob um `project_slug` comum e definir o `repo_slug` de cada um.
- [x] Validar que nós Graphify usam IDs estáveis
  `<project_slug>:<repo_slug>:<graphify_node_id>` — confirmado via
  `gpc/graphify_freshness.py` + `tests/test_multi_repo_graph_path.py`.
- [x] Rodar `gpc_graph_path` entre dois repos que compartilham um contrato
  — resultado esperado: caminho curto passando pelo símbolo compartilhado.
  Confirmado sinteticamente: caminho curto via `CROSS_REPO_BRIDGE` +
  `GRAPHIFY_RELATION`.
- [x] Medir freshness lag: tempo entre sync Graphify e disponibilidade no
  Neo4j — utilitário `gpc/graphify_freshness.py` (`measure_sync_freshness`),
  lag medido localmente em ~0.0017s. Falta medir o lag real
  commit→Neo4j num pipeline de produção real (depende de qual projeto for
  escolhido e de ter o post-commit hook instalado nele).

### 2.3 Auditoria cross-projeto

Depois de indexar os projetos reais adicionais escolhidos para a fase, rodar
uma passagem de análise com o próprio GPC (dogfooding):

A ferramenta de auditoria foi implementada e validada em 2026-09-17
(`gpc/cross_project_audit.py`, comando `gpc audit cross-project`). Rodada
ao vivo contra o único projeto registrado hoje (`gpc-local-verify`), gerando
`docs/audit-2026-09-17.md` sem gaps críticos. Falta rodar contra os 2+
projetos adicionais reais para uma auditoria cross-projeto completa.

- [x] `gpc_graph_summary` em cada projeto — registrar god nodes e communities.
  Automatizado por `gpc audit cross-project` (itera todos os projetos
  registrados via `graph_summary`).
- [x] Comparar convenções: naming, estrutura de config, modelo de embeddings
  (todos devem usar `nomic-embed-text:latest` —
  [AGENTS.md:19](../AGENTS.md#L19)). Comparação automática incluída no
  relatório gerado.
- [x] Listar gaps/inconsistências encontradas num documento
  `docs/audit-<date>.md` — gerado automaticamente pelo comando
  (`docs/audit-2026-09-17.md`).
- [ ] Criar issues para cada gap classificado como crítico. (Nenhum gap
  crítico foi detectado ainda porque só há 1 projeto registrado; pendente
  até termos gaps reais entre 3 projetos.)

### Prontidão da fase

- [ ] Três projetos indexados, com projeção Neo4j ativa. (Apenas 1 projeto
  real — `gpc-local-verify` — está indexado hoje; faltam 2 projetos
  externos reais.)
- [ ] Relatório de auditoria com recomendações priorizadas. (Ferramenta
  pronta e validada; falta rodá-la contra 3 projetos reais para produzir
  recomendações cross-projeto de fato.)
- [x] Zero vazamento de contexto entre projetos validado por smoke test
  dedicado — `tests/smoke/cross_project_isolation_smoke_test.py`
  (search, context, graph_neighbors/summary/path, incluindo rejeição de
  path adversarial entre projetos), passando ao vivo em 2026-09-17.

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
e `gpc_drift_signals(project)` (tool MCP opt-in).

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

**Implementado em 2026-09-17.** [scripts/auto_research.py](../scripts/auto_research.py)
gera uma proposta Markdown por projeto a partir de `gpc_drift_signals` e
`gpc_self_metrics`; não modifica código e não abre PRs. O módulo
[gpc/auto_research.py](../gpc/auto_research.py) torna a renderização testável.
Agendamento recomendado via cron externo (ver [operations.md](operations.md)).

### 3.4 Portfólio de testes derivados do grafo

Gerar testes smoke a partir da topologia:

- [x] Para cada god node com `calls`/`imports`/`imports_from` para um módulo
  externo, existe um arquivo de teste que referencia o módulo chamador?
  Implementado em 2026-09-17: [gpc/graph_test_portfolio.py](../gpc/graph_test_portfolio.py),
  comando `gpc graph test-portfolio`. Como efeito colateral, corrigiu um bug
  real em [gpc/graphify_sync.py](../gpc/graphify_sync.py) que descartava os
  campos `external`/`type` dos nós de conceito do Graphify ao persistir no
  Neo4j (necessários para distinguir símbolos externos).
- [x] Para cada `rationale_for` marcado EXTRACTED, existe implementação
  correspondente? Mesmo comando reporta `rationale_for_dangling`.

- [x] Discrepâncias viram entradas no relatório de drift — `--persist` grava
  cada gap como sinal `info` em `gpc_drift_signals`
  (`signal_type=graph_test_portfolio_gap`).

Rodada ao vivo em 2026-09-17 contra `gpc-local-verify`: 158 chamadas
externas checadas (132 cobertas), 94 `rationale_for` checados (0 dangling),
26 gaps — todos concentrados em `scripts/install_mcp_clients.py`, que ainda
não tinha teste dedicado. Corrigido no mesmo dia: adicionado
[tests/test_install_mcp_clients.py](../tests/test_install_mcp_clients.py)
(58 testes unitários cobrindo dispatch de clientes, manipulação de
config JSON, backup de arquivos e o fluxo de `main()`, tudo isolado do
filesystem/CLIs reais via `tmp_path`/`monkeypatch`). Rerodando o
portfólio após sincronizar o Graphify atualizado: 175 chamadas externas
checadas (155 cobertas), gap count caiu de 26 para 20 (nenhum mais em
`scripts/install_mcp_clients.py`; os 20 restantes estão em
`gpc/reset.py`, `gpc/status.py`, `gpc/token_economy.py`,
`scripts/auto_research.py` e `scripts/migrate.py`).

### 3.5 Sandbox de experimentação

Pasta `experiments/` (gitignored por default, exceto `README.md`) onde o
loop de auto-research pode escrever protótipos isolados sem tocar em
`gpc/`. Criada em 2026-09-17. Qualquer promoção
para `gpc/` passa por PR humano com `tests/smoke/` verde.

### Prontidão da fase

- [ ] `gpc_self_metrics` popula após cada run por pelo menos 2 semanas.
- [x] Primeiro `docs/proposals/` gerado automaticamente — ferramenta
  `scripts/auto_research.py` validada em 2026-09-17.
- [ ] Pelo menos uma proposta promovida a PR + merge após revisão humana,
  provando o loop fim-a-fim.

---

## Fase 4 — Produto comercial e monetização (proposta)

**Objetivo.** Evoluir o GPC de ferramenta local-first para um produto que
possa ser comercializado (self-host pago + cloud gerenciado), competindo no
nicho de contexto/memória para ferramentas de IA como Copilot, Codex e
Claude, sem abrir mão da premissa atual de MCP read-only e honestidade de
confidence.

Esta fase é estratégica/de negócio, não só técnica. As subseções abaixo
seguem o mesmo formato das fases anteriores (objetivo, entregáveis, arquivos,
prontidão), mas cobrem também decisões de arquitetura de produto, pricing e
licenciamento.

### 4.1 Multi-tenant e autenticação (P0)

- [ ] Introduzir `workspace_id`/`tenant_id` em todas as tabelas Postgres,
      coleções Qdrant e labels Neo4j hoje isolados só por `project_slug`.
- [ ] Login (OAuth GitHub/Google) e API keys por projeto, com rotação.
- [ ] Permissões por workspace (owner/editor/viewer).
- Arquivos prováveis: `gpc/db.py`, `migrations/`, novo módulo `gpc/auth.py`,
  `gpc/mcp_server.py` (gate de autenticação antes de cada tool).

### 4.2 Onboarding de baixa fricção (P0)

- [ ] Empacotar Postgres/Qdrant/Neo4j/Ollama num instalador único
      (binário ou imagem tudo-em-um), reduzindo a dependência de editar
      `.env` e rodar `docker compose` manualmente.
- [ ] `gpc login` / `gpc cloud` para o modo hospedado, com auto-config do
      MCP no client de IA detectado.
- [ ] Expandir `gpc install-clients` para cobrir extensões nativas de VS
      Code e JetBrains, não só edição de config JSON.
- Arquivos: `install.sh`, `gpc/cli.py`, novo módulo de provisioning cloud.

### 4.3 Billing e planos (P0)

- [ ] Integração com um provedor de billing (ex.: Stripe) e medição de uso
      (chunks indexados, tokens retornados, chamadas MCP por período).
- [ ] Planos Free / Pro / Team-Enterprise (ver tabela abaixo).
- [ ] Enforcement de limites do plano Free (nº de projetos, retenção de
      métricas) sem quebrar a promessa de dados no Postgres do usuário no
      modo self-host.

| Camada | Free | Pro (pago) | Team/Enterprise |
|---|---|---|---|
| Indexação local self-host | Ilimitada | Ilimitada | Ilimitada |
| Nº de projetos | Limitado (ex.: 3) | Ilimitado | Ilimitado |
| Busca semântica + contexto MCP | Básica | Com reranking | Com reranking + SLA |
| Grafo de relações (Neo4j) | Limitado/ausente | Sim | Sim + cross-repo avançado |
| Cloud hospedado | Não | Opcional | Padrão |
| SSO / múltiplos usuários | Não | Não | Sim |
| Suporte | Comunidade | E-mail | Dedicado + SLA |
| Observabilidade/auditoria | Básica (Grafana atual) | Completa | Completa + retenção customizada |

### 4.4 Segurança e compliance (P0)

- [ ] Criptografia em repouso para o modo cloud.
- [ ] Expandir a redação de segredos hoje presente no indexer para cobrir
      relatório de segurança vendável a empresas (referenciar
      [SECURITY.md](../SECURITY.md)).
- [ ] Avaliar trilha para certificação (ex.: SOC 2) se o modo cloud avançar.

### 4.5 Paridade competitiva com Augment (P1)

- [x] Watcher opcional em primeiro plano, com polling, debounce, retry e
      isolamento por worktree, além dos hooks de Git. Reconciliação validada
      localmente com serviços isolados; latência representativa continua pendente
      de medição. Ver [watcher](watcher.md).
- [x] Busca híbrida (léxica + semântica), preferências explícitas por tarefa e
      comparação em `gpc eval-search` com recall, MRR, nDCG e latência. Ganhos de
      relevância em produção não foram estabelecidos; ver [busca híbrida](hybrid-search.md).
- [x] Diagnóstico opcional de cobertura do índice e comparação dos três modos
      com `eval-search --compare-all --diagnose`, sem alterar denominadores das
      métricas. Fixture de desenvolvimento com 12 casos PT/EN e proveniência;
      não é benchmark representativo. Ver [guia de avaliação](retrieval-evaluation.md).
- [x] Análise opcional de candidatos e replay offline de limites por arquivo com
      `--analyze-candidates`, preservando o ranking padrão. Paridade de resultados
      validada no mesmo índice isolado; lacunas de candidatos e empates léxicos
      continuam pendentes. Ver [resultados](plans/2026-11-05-candidate-analysis.md).
- [x] Estratégia lexical ponderada experimental (`--lexical-strategy weighted`)
      e oito pares PT/EN revisados antes da medição. PostgreSQL isolado validado;
      ganhos em identificadores coexistem com regressão na híbrida em português
      natural. Padrões preservados; ver [experimento](plans/2026-11-05-weighted-lexical.md).
- [ ] Reranking aprendido e recuperação de símbolos via AST.
- [x] Memória de decisões com captura, aprovação, invalidação, supersessão e
      evidência de fonte indexada. CLI/MCP, migrações e ciclo de vida validados
      também em PostgreSQL local isolado; concorrência e desempenho representativos
      continuam pendentes. Ver [memória de decisões](decision-memory.md) e o
      [relatório OrbStack](plans/2026-11-04-orbstack-validation.md).
- [ ] Conectores de ADRs/PRs/issues e feedback de code review. A inspiração no
      Augment não estabelece equivalência funcional ou de qualidade.
- [ ] Dashboard web orientado a produto (além do Grafana técnico atual em
      [observability/](../observability)), legível por gestores não
      técnicos: economia de tokens, uso por cliente de IA, saúde do índice.
- [ ] Decisão consciente sobre não competir em "agente que escreve código":
      manter o GPC como camada de contexto que potencializa agentes
      existentes (Copilot, Codex, Claude, Cursor, Windsurf) via MCP, em vez
      de replicar o Cosmos/Auggie do Augment.

### 4.6 Diferenciação defensável (P2)

- [ ] Formalizar "privacidade por padrão" (100% local opcional,
      zero-retention no modo self-host) como proposta de valor central
      contra o processamento em nuvem do Augment.
- [ ] Reforçar interoperabilidade universal via MCP como diferencial
      ("funciona com qualquer cliente MCP") frente ao ecossistema mais
      fechado do Augment (Cosmos, Auggie).
- [ ] Tratar o grafo cross-repo (Neo4j/Graphify) como feature premium —
      já mais explícito publicamente do que o equivalente documentado pelo
      Augment.

### Licenciamento (decisão bloqueante antes de vender o modo cloud)

O GPC está sob **AGPLv3** ([LICENSE](../LICENSE), [NOTICE.md](../NOTICE.md)).
A AGPL obriga a disponibilizar o código-fonte de qualquer versão modificada
rodando como serviço de rede — isso afeta diretamente a Fase 4.2 (cloud
hospedado). Antes de lançar o modo cloud pago, decidir entre:

- Manter tudo em AGPL e vender apenas suporte/hospedagem gerenciada (modelo
  "aberto com serviço pago"); ou
- Adotar um modelo open-core: núcleo em AGPL (ou MIT) e extensões/SaaS sob
  licença comercial separada.

### Prontidão da fase

- [ ] Multi-tenant + autenticação funcionando em ambiente de staging.
- [ ] Ao menos um plano pago cobrando via billing real.
- [ ] Onboarding ponta a ponta (install → login → primeiro projeto indexado
      → primeira resposta MCP) medido em menos de 10 minutos sem suporte
      manual.
- [ ] Decisão de licenciamento documentada e comunicada em
      [NOTICE.md](../NOTICE.md) antes do primeiro cliente pago do modo cloud.

---

## Fora do escopo declarado

Coisas que já foram consideradas e conscientemente adiadas. Não são "TODO",
são "não agora":

- MCP chamando Neo4j no hot path de `gpc_search` / `gpc_context` — mantém a
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
