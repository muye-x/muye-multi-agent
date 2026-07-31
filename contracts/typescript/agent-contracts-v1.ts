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
  caller?: "agent-main";
  target_type?: "sub_agent";
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
  max_concurrency?: number;
  status: AgentCatalogStatus;
}

export interface AgentCatalogSnapshotV1 {
  schema_version: "muye.ai/agent-catalog-snapshot/v1";
  catalog_revision: string;
  catalog_checksum: string;
  agents?: AgentCatalogEntryV1[];
}

export interface SourceLocatorV1 {
  source_path: string;
  kind: "line" | "page" | "paragraph";
  start: number;
  end: number;
}

export interface ParsedBlockV1 {
  block_id: string;
  ordinal: number;
  content: string;
  locator: SourceLocatorV1;
}

export interface ParsedDocumentV1 {
  schema_version: "muye.ai/parsed-document/v1";
  knowledge_id: string;
  knowledge_version_id: string;
  document_id: string;
  source_file_id: string;
  source_path: string;
  source_checksum: string;
  parser_profile: string;
  blocks: ParsedBlockV1[];
}

export interface ChunkingPolicyV1 {
  max_characters: number;
  overlap_characters?: number;
  min_characters?: number;
}

export interface SchemaMetadataFieldV1 {
  name: string;
  type: "string" | "integer" | "boolean";
  filterable?: boolean;
  returnable?: boolean;
}

export interface SchemaProposalV1 {
  schema_version: "muye.ai/schema-proposal/v1";
  knowledge_id: string;
  knowledge_version_id: string;
  proposal_revision: string;
  proposal_checksum: string;
  parser_profile: string;
  embedding_alias: string;
  embedding_dimensions: number;
  chunking: ChunkingPolicyV1;
  metadata_fields?: SchemaMetadataFieldV1[];
  document_set_checksum: string;
}

export interface CollectionFieldPlanV1 {
  name: string;
  data_type: "VARCHAR" | "INT64" | "JSON" | "FLOAT_VECTOR" | "SPARSE_FLOAT_VECTOR";
  primary_key?: boolean;
  max_length?: number;
  dimension?: number;
  enable_analyzer?: boolean;
}

export interface MilvusIndexPlanV1 {
  field_name: string;
  index_type: "FLAT" | "SPARSE_INVERTED_INDEX";
  metric_type: "COSINE" | "IP" | "L2" | "BM25";
}

export interface CollectionIndexPlanV1 {
  schema_version: "muye.ai/collection-index-plan/v1";
  knowledge_id: string;
  knowledge_version_id: string;
  plan_revision: string;
  plan_checksum: string;
  collection_name: string;
  fields: CollectionFieldPlanV1[];
  bm25_function_name: "bm25_content";
  indexes: MilvusIndexPlanV1[];
}

export interface ResourceFieldMappingV1 {
  id: string;
  content: string;
  vector: string;
  keyword: string;
  exposed_fields?: Record<string, string>;
  filterable_fields?: Record<string, string>;
}

export interface PublishedPipelineV1 {
  type: "dense" | "keyword" | "hybrid";
  candidate_k?: number;
  dense_candidate_k?: number;
  keyword_candidate_k?: number;
  dense_weight?: number;
  keyword_weight?: number;
  rank_constant?: number;
  rerank_model?: string | null;
  rerank_required?: boolean;
}

export interface KnowledgeResourceManifestV1 {
  schema_version: "muye.ai/knowledge-resource-manifest/v1";
  resource_id: string;
  resource_revision: string;
  resource_checksum: string;
  knowledge_id: string;
  knowledge_version_id: string;
  collection_plan_checksum: string;
  connection: string;
  target: string;
  fields: ResourceFieldMappingV1;
  embedding_alias: string;
  embedding_dimensions: number;
  pipelines: Record<string, PublishedPipelineV1>;
  default_pipeline: string;
  default_return_fields: string[];
}

export interface ResourceSnapshotV1 {
  schema_version: "muye.ai/resource-snapshot/v1";
  snapshot_revision: string;
  snapshot_checksum: string;
  resources: Record<string, KnowledgeResourceManifestV1>;
}

export interface EvaluationCaseV1 {
  case_id: string;
  query: string;
  relevant_chunk_ids: string[];
  required_citation_ids?: string[];
}

export interface EvaluationSetV1 {
  schema_version: "muye.ai/evaluation-set/v1";
  evaluation_set_id: string;
  revision: string;
  checksum: string;
  recall_at_k?: number;
  min_recall: number;
  min_mrr: number;
  min_citation_coverage: number;
  cases: EvaluationCaseV1[];
}

export interface KnowledgeJobV1 {
  schema_version: "muye.ai/knowledge-job/v1";
  job_id: string;
  kind: "build" | "evaluate";
  knowledge_slug: string;
  status: "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED";
  attempt: number;
  created_at: string;
  updated_at: string;
  input_checksum: string;
  report_ref?: string | null;
  error_code?: string | null;
}

export type ContractSchemaName =
  | "agent-build-record-v1"
  | "agent-catalog-snapshot-v1"
  | "agent-descriptor-v1"
  | "agent-generation-spec-v1"
  | "collection-index-plan-v1"
  | "evaluation-set-v1"
  | "knowledge-job-v1"
  | "knowledge-resource-manifest-v1"
  | "parsed-document-v1"
  | "resource-snapshot-v1"
  | "schema-proposal-v1"
  | "source-provenance-v1";
