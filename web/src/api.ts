import type { Project, Session, SkillCatalog, TimelineEvent, User } from './types';

const API_ROOT = '/api/v1';

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, detail: unknown) {
    super(typeof detail === 'string' ? detail : `Request failed with status ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, token: string | null, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (!headers.has('Content-Type') && init.body) {
    headers.set('Content-Type', 'application/json');
  }
  if (token) {
    headers.set('Authorization', `Bearer ${token}`);
  }
  const response = await fetch(`${API_ROOT}${path}`, { ...init, headers });
  if (!response.ok) {
    let detail: unknown = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail ?? body;
    } catch {
      detail = await response.text();
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return response.json() as Promise<T>;
}

export const api = {
  async login(username: string, password: string) {
    return request<{ access_token: string; expires_at: string }>('/auth/login', null, {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });
  },

  me(token: string) {
    return request<User>('/me', token);
  },

  skills(token: string) {
    return request<SkillCatalog>('/skills/catalog', token);
  },

  projects(token: string) {
    return request<Project[]>('/projects', token);
  },

  createProject(token: string, name: string, skillNames: string[]) {
    return request<Project>('/projects', token, {
      method: 'POST',
      body: JSON.stringify({ name, skill_names: skillNames }),
    });
  },

  createProjectSession(token: string, projectId: string) {
    return request<Session>(`/projects/${projectId}/sessions`, token, {
      method: 'POST',
      body: JSON.stringify({}),
    });
  },

  getSession(token: string, sessionId: string) {
    return request<Session>(`/sessions/${sessionId}`, token);
  },

  deleteSession(token: string, sessionId: string) {
    return request<Session>(`/sessions/${sessionId}`, token, { method: 'DELETE' });
  },

  execute(token: string, sessionId: string, task: string) {
    return request<{ conversation_id: string | null; status: string }>(`/sessions/${sessionId}/execute`, token, {
      method: 'POST',
      body: JSON.stringify({ task }),
    });
  },

  history(token: string, sessionId: string) {
    return request<{ events: TimelineEvent[]; next_page_id: string | null }>(`/sessions/${sessionId}/history?limit=100`, token);
  },

  async stream(
    token: string,
    sessionId: string,
    after: string | null,
    onEvent: (event: TimelineEvent) => void,
    signal: AbortSignal,
  ) {
    const query = after ? `?after=${encodeURIComponent(after)}` : '';
    const response = await fetch(`${API_ROOT}/sessions/${sessionId}/stream${query}`, {
      headers: { Authorization: `Bearer ${token}` },
      signal,
    });
    if (!response.ok || !response.body) {
      throw new ApiError(response.status, response.statusText);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (!signal.aborted) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split(/\r?\n\r?\n/);
      buffer = chunks.pop() ?? '';
      for (const chunk of chunks) {
        const data = chunk
          .split(/\r?\n/)
          .filter((line) => line.startsWith('data:'))
          .map((line) => line.slice(5).trimStart())
          .join('\n');
        if (!data) continue;
        try {
          onEvent(JSON.parse(data) as TimelineEvent);
        } catch {
          onEvent({ kind: 'client.parse_error', data: { raw: data } });
        }
      }
    }
  },
};

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (typeof error.detail === 'string') return error.detail;
    return JSON.stringify(error.detail);
  }
  if (error instanceof Error) return error.message;
  return String(error);
}
