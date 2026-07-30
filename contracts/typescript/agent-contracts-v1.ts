/**
 * v2.0 模板 Agent 的前端/Control Server DTO。
 *
 * 运行时校验必须使用同目录上层 `schemas/` 内的 JSON Schema；本文件只为 TypeScript
 * 调用方提供静态类型，不能替代边界校验。字段与 Pydantic 契约共用 `fixtures/` 正反例。
 */

export type AgentCatalogStatus =
  | "DISCOVERED"
  | "STARTING"
  | "ACTIVE"
  | "DEGRADED"
  | "INACTIVE"
  | "REJECTED";

export interface ResourceBindingV1 {
  resource_id: string;
  skill_ref: string;
}

export interface AgentRuntimeV1 {
  internal_port: number;
  timeout_seconds: number;
  token_budget: number;
  tool_budget: number;
  max_concurrency: number;
  memory_limit: string;
}

export interface AgentDeploymentV1 {
  enabled: boolean;
}

export interface AgentSourceV1 {
  template_id: string;
  template_version: string;
  provenance_file: ".muye-generation.json";
}

export interface AgentDescriptorV1 {
  schema_version: "muye.ai/agent-descriptor/v1";
  agent_id: string;
  slug: string;
  tool_name: string;
  display_name: string;
  version: string;
  description: string;
  supported_intents: string[];
  entrypoint: "main:app";
  api_profile: "internal";
  protocol_version: string;
  model_alias: string;
  resources: ResourceBindingV1[];
  runtime: AgentRuntimeV1;
  deployment: AgentDeploymentV1;
  source: AgentSourceV1;
}

export interface AgentGenerationSpecV1 {
  schema_version: "muye.ai/agent-generation-spec/v1";
  agent_id: string;
  slug: string;
  template_id: string;
  template_version: string;
  sdk_version: string;
  agent_profile_revision: string;
  agent_profile_checksum: string;
  resource_id: string;
  resource_revision: string;
  skill_revision: string;
  skill_checksum: string;
  model_alias: string;
  retrieval_pipeline: string;
  scope_filter_ref: string;
  allowed_filter_fields?: string[];
  allowed_return_fields: string[];
  tool_budget: number;
  token_budget: number;
  timeout_budget_seconds: number;
  evaluation_set_ref: string;
  input_checksum: string;
}

export interface SourceProvenanceV1 {
  schema_version: "muye.ai/source-provenance/v1";
  generator_version: string;
  template_id: string;
  template_version: string;
  sdk_version: string;
  generation_spec_checksum: string;
  knowledge_resource_checksum: string;
  skill_checksum: string;
  profile_checksum: string;
  generated_at: string;
  generated_files: string[];
  generated_source_tree_checksum: string;
}

export interface AgentBuildRecordV1 {
  schema_version: "muye.ai/agent-build-record/v1";
  build_record_id: string;
  agent_id: string;
  agent_version: string;
  descriptor_checksum: string;
  source_tree_checksum: string;
  sdk_version: string;
  base_image_digest: string;
  image_digest: string;
  sbom_ref: string;
  test_report_ref: string;
  built_at: string;
  builder_version: string;
}

export interface AgentCatalogEntryV1 {
  agent_id: string;
  agent_version: string;
  tool_name: string;
  display_name: string;
  description: string;
  supported_intents: string[];
  service_name: string;
  base_url: string;
  timeout_seconds: number;
  internal_protocol_version: string;
  api_profile: "internal";
  descriptor_checksum: string;
  source_tree_checksum: string;
  image_digest: string;
  resource_bindings: ResourceBindingV1[];
  capabilities_checksum: string;
  status: AgentCatalogStatus;
}

export interface AgentCatalogSnapshotV1 {
  schema_version: "muye.ai/agent-catalog-snapshot/v1";
  catalog_revision: string;
  catalog_checksum: string;
  agents?: AgentCatalogEntryV1[];
}

export type ContractSchemaName =
  | "agent-build-record-v1"
  | "agent-catalog-snapshot-v1"
  | "agent-descriptor-v1"
  | "agent-generation-spec-v1"
  | "source-provenance-v1";
