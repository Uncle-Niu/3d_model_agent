/**
 * TypeScript types matching backend domain models.
 */

export interface Project {
  project_id: string;
  name: string;
  created_at: string;
  updated_at: string;
  project_path: string;
  hard_constraints: HardConstraints;
  soft_constraints: SoftConstraints;
}

export interface HardConstraints {
  max_x_mm: number;
  max_y_mm: number;
  max_z_mm: number;
  min_wall_thickness_mm: number;
  max_file_size_mb: number;
}

export interface SoftConstraints {
  overhang_angle_max: number;
  prefer_fillets: boolean;
  prefer_chamfers: boolean;
  material: string;
  notes: string;
}

export interface GlobalSettings {
  hard_constraints: HardConstraints;
  soft_constraints: SoftConstraints;
}

export interface ModelInfo {
  model_id: string;
  parent_model_id?: string;
  created_at: string;
  prompt: string;
  has_step: boolean;
  has_stl: boolean;
  has_glb: boolean;
  iteration: number;
  failure_type?: string | null;
  vision_score?: number | null;
  is_final?: boolean;
  thread_id?: string | null;
  turn_index?: number | null;
}

export interface PipelineStep {
  stage: string;
  message: string;
  details?: string;
  data?: {
    why?: string;
    rationale?: string;
    used?: string[];
    skipped?: string[];
    outcome?: string;
    sub_stage?: string;
    iteration?: number;
    in_progress?: boolean;
    reasoning_channel?: string;
    [key: string]: unknown;
  };
  timestamp: string;
}

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
  model_id?: string;
  steps?: PipelineStep[];
  reasoning?: string;
  agent_logic?: AgentLogic;
}

export type AgentLogic = 'orchestrator' | 'llm_agent';

export interface ChatThreadSummary {
  thread_id: string;
  title: string;
  created_at: string | null;
  updated_at: string | null;
  message_count: number;
  last_message?: ChatMessage | null;
}

export interface ChatThread {
  thread_id: string;
  title: string;
  created_at: string | null;
  updated_at: string | null;
  messages: ChatMessage[];
}

// WebSocket message types (server → client)
export interface WSStatus {
  type: 'status';
  stage: string;
  message: string;
  details?: string;
  data?: Record<string, unknown>;
}

export interface WSModelReady {
  type: 'model_ready';
  model_id: string;
  glb_url: string;
}

export interface WSLLMChunk {
  type: 'llm_chunk';
  content: string;
}

export interface WSChatResponse {
  type: 'chat_response';
  content: string;
  reasoning?: string;
  model_id?: string;
  steps?: PipelineStep[];
}

export interface WSError {
  type: 'error';
  message: string;
  failure_type?: string;
}

export interface WSDebugLog {
  type: 'debug_log';
  timestamp: string;
  category: string;
  message: string;
  data?: Record<string, unknown>;
}

export interface GeometryIssue {
  issue_type: string;
  severity: 'error' | 'warning' | 'info';
  description: string;
  location_hint: string;
}

export interface WSCritiqueResult {
  type: 'critique_result';
  score: number;
  matches_intent: boolean;
  issues: GeometryIssue[];
  repair_prompt: string;
  render_urls: Record<string, string>;  // view_name → REST URL
}

export interface WSReasoningChunk {
  type: 'reasoning_chunk';
  channel: string;
  content: string;
}

export interface WSRunState {
  type: 'run_state';
  running: boolean;
  project_id: string;
  thread_id: string;
  agent_logic?: AgentLogic;
  started_at?: string;
  steps?: PipelineStep[];
}

export type WSMessage =
  | WSStatus
  | WSModelReady
  | WSLLMChunk
  | WSChatResponse
  | WSError
  | WSDebugLog
  | WSCritiqueResult
  | WSReasoningChunk
  | WSRunState;

// Debug log entry for the store
export interface DebugEntry {
  id: number;
  timestamp: string;
  category: string;
  message: string;
  data?: Record<string, unknown>;
}

// Critique state stored in Zustand
export interface CritiqueState {
  score: number;
  matchesIntent: boolean;
  issues: GeometryIssue[];
  renderUrls: Record<string, string>;
}

// CAD features and parameters
export interface CadParameter {
  name: string;
  value: string | number | boolean;
  type: 'float' | 'int' | 'str' | 'bool';
  description?: string;
  min_value?: number;
  max_value?: number;
}

export interface FeatureManifest {
  name: string;
  type: string;
  center: [number, number, number];
}

export interface GeometryStats {
  bbox_x_mm?: number;
  bbox_y_mm?: number;
  bbox_z_mm?: number;
  volume_mm3?: number;
  surface_area_mm2?: number;
  solid_count: number;
  face_count: number;
  edge_count: number;
  is_closed: boolean;
  estimated_mass_g?: number;
  center_of_mass_x?: number;
  center_of_mass_y?: number;
  center_of_mass_z?: number;
}

export interface ManufacturabilityIssue {
  issue_type: string;
  severity: 'error' | 'warning';
  description: string;
  location_hint?: string;
}

export interface ManufacturabilityReport {
  issues: ManufacturabilityIssue[];
  is_printable: boolean;
  score: number;
}

export interface AssemblyPart {
  name: string;
  color?: string;
  material?: string;
  geometry_stats?: GeometryStats;
  manufacturability?: ManufacturabilityReport;
  visible: boolean;
}

export interface AssemblyManifest {
  parts: AssemblyPart[];
  total_parts: number;
}
