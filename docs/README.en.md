# Documentation Index

[中文文档入口](../README.md) | [English project README](../README.en.md)

The canonical detailed documentation in this repository is currently maintained
in Chinese. This index provides English descriptions and links so English-speaking
developers can select the relevant document without changing the default Chinese
documentation experience.

## Getting started

| Document | What it covers |
| --- | --- |
| [Create and test a knowledge Agent](agent-creation-quickstart.md) | Stepwise workflow for planning, approving, building, evaluating, generating, and locally integrating a knowledge Agent. |
| [Agent project inputs](../agent-projects/README.md) | Version-controlled `project.yaml` and source-material inputs for `agent prepare`. |
| [Template Agent Generator and developer ownership](v2.0-agent-generator.md) | Template generation contract, developer takeover, provenance, validation, and template upgrades. |
| [Knowledge pipeline and evaluation](v2.0-knowledge-pipeline.md) | Source parsing, immutable Milvus versions, retrieval evaluation gates, Resource Snapshot publication, and cancellation/retry behavior. |

## Production and operations

| Document | What it covers |
| --- | --- |
| [Agent Catalog, grants, and deployment](v2.0-agent-catalog.md) | Catalog lifecycle, user grants, deployment, rollback, health checks, and failure behavior. |
| [Administrator guide](v2.0-admin-guide.md) | Built-in administrator responsibilities, user grants, and Agent states. |
| [Operations guide](v2.0-operations.md) | Compose startup, configuration, backups, logging, smoke checks, and production network boundaries. |
| [Migration guide](v2.0-migration.md) | Moving from the fixed Travel/Order services to generated knowledge Agents. |
| [Release checklist](v2.0-release-checklist.md) | Immutable release evidence, validation gates, deployment verification, and backup recovery checks. |

## Architecture decisions

| ADR | Decision |
| --- | --- |
| [ADR-0001](adr/0001-template-agent-source-ownership.md) | Generated Agent source is developer-owned after initial deterministic generation. |
| [ADR-0002](adr/0002-mainagent-topology-and-user-agent-grants.md) | MainAgent is the only SubAgent caller; users receive explicit per-Agent grants. |
| [ADR-0003](adr/0003-catalog-and-compose-artifact-lifecycle.md) | Catalog and Compose artifacts have a reproducible, checksum-validated lifecycle. |

## Historical records

The following Chinese documents are historical design and review records. They
are retained unchanged so their original decisions, timestamps, and audit context
remain intact:

- [v2.0 development review log](v2.0-development-review-log.md)
- [v2.0 template-agent implementation plan](v2.0-template-agent-implementation-plan.md)
