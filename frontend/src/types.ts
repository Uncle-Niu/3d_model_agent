/**
 * TypeScript types matching backend domain models.
 */

export interface Project {
  project_id: string;
  name: string;
  created_at: string;
  updated_at: string;
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

export type WSMessage =
  | WSStatus
  | WSModelReady
  | WSLLMChunk
  | WSChatResponse
  | WSError;
