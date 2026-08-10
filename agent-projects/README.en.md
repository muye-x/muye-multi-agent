# Agent Project Inputs

[中文说明](README.md)

`agent-projects/` contains version-controlled inputs for Agent creation. Every
subdirectory must contain `project.yaml` and `sources/`, and can be passed
directly to `agent prepare`:

```bash
./scripts/muye.sh agent prepare agent-projects/<slug> \
  --auto-approved-by <reviewer> \
  --dev
```

`hotel-employee/` is the scaffold's built-in example. It contains an employee
handbook and its Agent definition, demonstrating the full path from source
materials to a knowledge base, generated SubAgent, and local integration.

Generated source lives in `agents/agent-<slug>/`. Milvus Collections, MinIO data,
creation plans, evaluation reports, and local runtime files are generated or
runtime artifacts and do not belong in this directory. Do not write secrets,
tokens, database connection strings, or other environment configuration to
`project.yaml` or `sources/`.
