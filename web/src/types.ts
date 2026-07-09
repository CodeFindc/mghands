export type User = {
  user_id: string;
  username: string;
  role: 'admin' | 'user';
  enabled: boolean;
};

export type Project = {
  project_id: string;
  name: string;
  status: 'active' | 'deleted';
  created_at: string;
  updated_at: string;
};

export type Session = {
  session_id: string;
  project_id: string | null;
  sandbox_id: string | null;
  sandbox_url: string | null;
  conversation_id: string | null;
  status: 'created' | 'running' | 'completed' | 'error' | 'deleted';
  created_at: string;
  updated_at: string;
  last_event_id: string | null;
  error: string | null;
};

export type SkillCatalogItem = {
  name: string;
  valid: boolean;
  error?: string | null;
  metadata?: {
    description?: string | null;
    requires_dependencies?: boolean;
    dependency_status?: string | null;
    dependencies?: string[];
    triggers?: string[];
  };
};

export type SkillCatalog = {
  default_project_skills: string[];
  items: SkillCatalogItem[];
};

export type TimelineEvent = {
  id?: string;
  kind?: string;
  timestamp?: string;
  data?: Record<string, unknown>;
  [key: string]: unknown;
};

export type SystemSettings = Record<string, string>;

export type LLMModel = {
  model_id: string;
  name: string;
  provider: string;
  model: string;
  base_url: string | null;
  api_key: string | null;
  is_default: boolean;
  created_at: string;
  updated_at: string;
};

export type WorkspaceFile = {
  path: string;
  is_dir: boolean;
  size: number;
  updated_at: string;
};
