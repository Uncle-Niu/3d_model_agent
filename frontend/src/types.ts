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

export interface ModelInfo {
  model_id: string;
  created_at: string;
  prompt: string;
  has_step: boolean;
  has_stl: boolean;
  has_glb: boolean;
  iteration: number;
}

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
  model_id?: string;
}

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

export type WSMessage =
  | WSStatus
  | WSModelReady
  | WSLLMChunk
  | WSChatResponse
  | WSError
  | WSDebugLog;

// Debug log entry for the store
export interface DebugEntry {
  id: number;
  timestamp: string;
  category: string;
  message: string;
  data?: Record<string, unknown>;
}
