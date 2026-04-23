# Open Source Notice

GPC — Global Project Context is licensed under the GNU Affero General Public
License v3.0 or later. See [LICENSE](LICENSE) for the full text.

Author: Rodrigo Alves
LinkedIn: <https://www.linkedin.com/in/rodrigomarquesalves>

## License Intent

The intent is to keep the project's code, documentation and any
network-exposed modifications open source. AGPLv3 is a strong copyleft
license and is especially appropriate for software that can be exposed over a
network (such as the MCP HTTP server in this repository).

## Limits of This License

- Open-source licenses cannot prohibit commercial use and still qualify as
  open source. AGPLv3 is no exception.
- Copyright protects the concrete code, documentation and other authored
  expression in this repository. It does not protect the abstract idea,
  concept, method or architecture behind the project.
- No trademark rights are granted for the names *GPC* or *Global Project
  Context*.

If you need to enforce brand usage or commercial agreements, handle that
separately through trademark policy, contracts or a dual-license commercial
offering.

## Third-Party Components

GPC depends on several third-party services and libraries, each under its own
license:

| Component | Role | License |
|---|---|---|
| [Postgres](https://www.postgresql.org/about/licence/) (with `pgvector`) | Source of truth for indexed metadata. | PostgreSQL License (BSD-style). |
| [Qdrant](https://github.com/qdrant/qdrant/blob/master/LICENSE) | Semantic vector store. | Apache 2.0. |
| [Ollama](https://github.com/ollama/ollama/blob/main/LICENSE) | Local embedding runtime. | MIT. |
| [Neo4j Community](https://neo4j.com/licensing/) | Graph projection store. | GPL v3 (Community Edition). |
| Python dependencies in `requirements.txt` | See individual package metadata. | Various permissive licenses. |

The default embedding model `nomic-embed-text` is published by Nomic; see the
[Ollama model page](https://ollama.com/library/nomic-embed-text) for its
license.

The static landing page under `site/` references icons from
[Simple Icons](https://github.com/simple-icons/simple-icons) (CC0) loaded via
the jsDelivr CDN.

## Reporting Trademark or License Concerns

For trademark or licensing questions, contact the maintainer through the
channels listed in [SECURITY.md](SECURITY.md#reporting-a-vulnerability).
