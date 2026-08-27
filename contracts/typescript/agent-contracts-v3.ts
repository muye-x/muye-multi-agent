/**
 * v3.0 Platform 的前端和 Runtime DTO。
 *
 * 运行时边界必须以同目录上层 schemas/ 内的 JSON Schema 校验；这里仅提供静态类型。
 * Python、JSON Schema 和 TypeScript 通过 fixtures/ 正反例共同回归。
 */

export interface RevisionSourceAssetV1 {
  asset_id: string;
  sha256: string;
  display_name: string;
}

export interface AgentModelConfigV1 {
  chat_alias: string;
  embedding_alias: string;
  temperature: number;
}

export interface AgentRetrievalConfigV1 {
  pipeline: "dense" | "keyword" | "hybrid";
  top_k: number;
  rerank_alias?: string | null;
  minimum_score: number;
}

export interface AgentBudgetV1 {
  output_tokens: number;
  tool_calls: number;
  timeout_seconds: number;
}

export interface AgentEvaluationCaseV1 {
  case_id: string;
  question: string;
  expected_source_asset_ids: string[];
}

export interface AgentEvaluationConfigV1 {
  cases: AgentEvaluationCaseV1[];
  minimum_pass_rate: number;
  citation_required: boolean;
}

export interface AgentRevisionSpecV1 {
  schema_version: "muye.ai/agent-revision/v1";
  agent_id: string;
  revision_id: string;
  revision_number: number;
  display_name: string;
  objective: string;
  instructions: string;
  prohibited_actions: string[];
  examples: string[];
  model: AgentModelConfigV1;
  retrieval: AgentRetrievalConfigV1;
  budgets: AgentBudgetV1;
  source_assets: RevisionSourceAssetV1[];
  evaluation: AgentEvaluationConfigV1;
}

export interface RuntimeResourceBindingV1 {
  resource_id: string;
  collection_name: string;
  collection_checksum: string;
  embedding_alias: string;
}

export interface AgentRevisionBundleManifestV1 {
  schema_version: "muye.ai/agent-revision-bundle/v1";
  agent_id: string;
  revision_id: string;
  revision_checksum: string;
  bundle_checksum: string;
  build_id: string;
  runtime_contract_version: "muye-runtime/1";
  resources: RuntimeResourceBindingV1[];
}

export interface JobEventV1 {
  schema_version: "muye.ai/job-event/v1";
  job_id: string;
  sequence: number;
  event_type: "started" | "progress" | "artifact" | "completed" | "failed" | "cancelled";
  emitted_at: string;
  stage: string;
  message?: string | null;
  progress_current?: number | null;
  progress_total?: number | null;
  artifact_ref?: string | null;
  error_code?: string | null;
}

export interface RuntimeCitationV1 {
  citation_id: string;
  source_asset_id: string;
  locator: string;
}

export interface RuntimeInvokeRequestV1 {
  schema_version: "muye.ai/runtime-invoke-request/v1";
  request_id: string;
  session_id: string;
  user_id: string;
  task: string;
}

export interface RuntimeInvokeResponseV1 {
  schema_version: "muye.ai/runtime-invoke-response/v1";
  request_id: string;
  status: "success" | "refused" | "error";
  content?: string | null;
  citations?: RuntimeCitationV1[];
  error_code?: string | null;
  error_message?: string | null;
}

export interface RuntimeCancelRequestV1 {
  schema_version: "muye.ai/runtime-cancel-request/v1";
  request_id: string;
  reason: "client_disconnect" | "timeout" | "deployment_drain" | "operator";
}

export interface RuntimeCapabilitiesV1 {
  schema_version: "muye.ai/runtime-capabilities/v1";
  agent_id: string;
  revision_id: string;
  revision_checksum: string;
  runtime_contract_version: "muye-runtime/1";
  supports_streaming: true;
  supports_cancel: true;
}

export interface ChatStreamEventV1 {
  schema_version: "muye.ai/chat-stream-event/v1";
  event_type: "session_start" | "block_delta" | "thinking_delta" | "tool_start" | "tool_update" | "tool_complete" | "done" | "error" | "session_end";
  sequence: number;
  session_id: string;
  block_id?: string | null;
  delta?: string | null;
  tool_call_id?: string | null;
  tool_name?: string | null;
  citations?: RuntimeCitationV1[];
  error_code?: string | null;
  message?: string | null;
  total_tokens?: number | null;
}
