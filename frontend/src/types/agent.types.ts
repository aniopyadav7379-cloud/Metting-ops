// Agent type definitions shared across components

export interface Agent {
  id: string;
  name: string;
  slug: string;
  description: string;
  icon: string;
  agent_type: AgentType;
  purpose: string;
  is_core: boolean;
  is_active: boolean;
  is_default_live: boolean;
  is_default_final: boolean;
  provider_type: string;
  model_name: string;
  model_endpoint?: string;
  model_settings: ModelSettings;
  system_prompt: string;
  output_format: string;
  output_template?: any;
  progressive_config?: any;
  chat_config?: any;
  permission_level: string;
  performance_metrics?: {
    avg_tokens_per_sec: number;
    model_size: string;
    context_window: string;
  };
  created_at?: string;
  updated_at?: string;
  usage_count?: number;
}

export enum AgentType {
  CORE_LIVE = "core_live",
  CORE_FINAL = "core_final",
  TASK_TEMPLATE = "task_template",
  CHAT_SINGLE = "chat_single",
  CHAT_CROSS = "chat_cross",
  CHAT_ALL = "chat_all",
  EXPORT = "export",
  ANALYSIS = "analysis"
}

export interface ModelSettings {
  temperature: number;
  max_tokens: number;
  top_p: number;
  context_window: number;
}

export interface ModelPerformance {
  model: string;
  speed: string;
  tokens_per_sec: number;
  context: string;
  memory: string;
  best_for: string;
}