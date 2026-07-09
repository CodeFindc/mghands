import { FormEvent, useEffect, useMemo, useRef, useState } from 'react';
import {
  Bot,
  CheckCircle2,
  CircleStop,
  FolderPlus,
  KeyRound,
  Loader2,
  LogOut,
  MessageSquareText,
  Plus,
  RadioTower,
  SendHorizontal,
  Sparkles,
  TerminalSquare,
  Wrench,
} from 'lucide-react';
import { ApiError, api, errorMessage } from './api';
import type { Project, Session, SkillCatalogItem, TimelineEvent, User } from './types';

const TOKEN_KEY = 'mghands.access_token';
const SESSION_MAP_KEY = 'mghands.project_sessions';

type SessionMap = Record<string, string>;

function loadSessionMap(): SessionMap {
  try {
    return JSON.parse(localStorage.getItem(SESSION_MAP_KEY) || '{}') as SessionMap;
  } catch {
    return {};
  }
}

function saveSessionMap(map: SessionMap) {
  localStorage.setItem(SESSION_MAP_KEY, JSON.stringify(map));
}

function eventTitle(event: TimelineEvent): string {
  const kind = event.kind || 'event';
  if (kind === 'message') return '用户消息';
  if (kind === 'agent.result') return '运行结果';
  if (kind === 'agent.error') return '运行错误';
  if (kind.includes('ActionEvent')) return '工具调用';
  if (kind.includes('ObservationEvent')) return '工具结果';
  if (kind.includes('MessageEvent')) return '模型消息';
  return kind;
}

function eventPreview(event: TimelineEvent): string {
  const data = event.data || {};
  const preview = data.preview;
  const message = data.message || data.content || data.result || data.error || data.detail;
  if (typeof preview === 'string' && preview.trim()) return preview;
  if (typeof message === 'string' && message.trim()) return message;
  return JSON.stringify(data, null, 2);
}

function statusLabel(status?: string | null): string {
  if (status === 'created') return '已创建';
  if (status === 'running') return '运行中';
  if (status === 'completed') return '已完成';
  if (status === 'error') return '错误';
  if (status === 'deleted') return '已删除';
  return '未连接';
}

export default function App() {
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_KEY) || '');
  const [user, setUser] = useState<User | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [skills, setSkills] = useState<SkillCatalogItem[]>([]);
  const [defaultSkills, setDefaultSkills] = useState<string[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(null);
  const [selectedSkillNames, setSelectedSkillNames] = useState<string[]>([]);
  const [session, setSession] = useState<Session | null>(null);
  const [events, setEvents] = useState<TimelineEvent[]>([]);
  const [prompt, setPrompt] = useState('');
  const [projectName, setProjectName] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [sessionMap, setSessionMap] = useState<SessionMap>(() => loadSessionMap());
  const abortRef = useRef<AbortController | null>(null);

  const selectedProject = useMemo(
    () => projects.find((project) => project.project_id === selectedProjectId) || null,
    [projects, selectedProjectId],
  );

  const lastEventId = events.length ? String(events[events.length - 1].id || '') : null;

  useEffect(() => {
    if (!token) return;
    void bootstrap(token);
  }, [token]);

  useEffect(() => {
    if (!token || !selectedProjectId) return;
    const sessionId = sessionMap[selectedProjectId];
    if (!sessionId) {
      setSession(null);
      setEvents([]);
      return;
    }
    void api
      .getSession(token, sessionId)
      .then((next) => {
        setSession(next);
        if (next.conversation_id) void refreshHistory(next.session_id);
      })
      .catch(() => {
        setSession(null);
        setEvents([]);
      });
  }, [selectedProjectId, sessionMap, token]);

  async function bootstrap(nextToken: string) {
    try {
      setBusy(true);
      const [me, catalog, projectList] = await Promise.all([
        api.me(nextToken),
        api.skills(nextToken),
        api.projects(nextToken),
      ]);
      setUser(me);
      setSkills(catalog.items || []);
      setDefaultSkills(catalog.default_project_skills || []);
      setSelectedSkillNames(catalog.default_project_skills || []);
      setProjects(projectList);
      setSelectedProjectId((current) => current || projectList[0]?.project_id || null);
      setNotice(null);
    } catch (error) {
      setNotice(errorMessage(error));
      if (error instanceof ApiError && error.status === 401) logout();
    } finally {
      setBusy(false);
    }
  }

  async function handleLogin(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      const result = await api.login(username, password);
      localStorage.setItem(TOKEN_KEY, result.access_token);
      setToken(result.access_token);
      setNotice(null);
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  function logout() {
    abortRef.current?.abort();
    localStorage.removeItem(TOKEN_KEY);
    setToken('');
    setUser(null);
    setProjects([]);
    setSession(null);
    setEvents([]);
  }

  async function createProject(event: FormEvent) {
    event.preventDefault();
    if (!token || !projectName.trim()) return;
    setBusy(true);
    try {
      const project = await api.createProject(token, projectName.trim(), selectedSkillNames);
      setProjects((items) => [project, ...items]);
      setSelectedProjectId(project.project_id);
      setProjectName('');
      setNotice('项目已创建');
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function ensureSession(): Promise<Session | null> {
    if (!token || !selectedProject) return null;
    if (session && session.status !== 'deleted') return session;
    setBusy(true);
    try {
      const next = await api.createProjectSession(token, selectedProject.project_id);
      const map = { ...sessionMap, [selectedProject.project_id]: next.session_id };
      saveSessionMap(map);
      setSessionMap(map);
      setSession(next);
      setNotice('沙箱会话已创建');
      return next;
    } catch (error) {
      if (error instanceof ApiError && error.status === 409 && typeof error.detail === 'object' && error.detail) {
        const runningId = (error.detail as { running_session_id?: string }).running_session_id;
        if (runningId) {
          const next = await api.getSession(token, runningId);
          const map = { ...sessionMap, [selectedProject.project_id]: runningId };
          saveSessionMap(map);
          setSessionMap(map);
          setSession(next);
          return next;
        }
      }
      setNotice(errorMessage(error));
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function stopSession() {
    if (!token || !session || !selectedProject) return;
    setBusy(true);
    try {
      await api.deleteSession(token, session.session_id);
      const map = { ...sessionMap };
      delete map[selectedProject.project_id];
      saveSessionMap(map);
      setSessionMap(map);
      setSession(null);
      setEvents([]);
      setNotice('会话已停止');
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function runPrompt(event: FormEvent) {
    event.preventDefault();
    if (!token || !prompt.trim()) return;
    const active = await ensureSession();
    if (!active) return;
    const text = prompt.trim();
    setPrompt('');
    setEvents((items) => [...items, { kind: 'message', timestamp: new Date().toISOString(), data: { message: text } }]);
    setBusy(true);
    try {
      await api.execute(token, active.session_id, text);
      const next = await api.getSession(token, active.session_id);
      setSession(next);
      await refreshHistory(active.session_id);
      if (next.conversation_id) startStream(active.session_id);
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function refreshHistory(sessionId = session?.session_id) {
    if (!token || !sessionId) return;
    try {
      const history = await api.history(token, sessionId);
      setEvents(history.events || []);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) return;
      setNotice(errorMessage(error));
    }
  }

  function startStream(sessionId = session?.session_id) {
    if (!token || !sessionId) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setStreaming(true);
    void api
      .stream(token, sessionId, lastEventId, (event) => {
        setEvents((items) => {
          if (event.id && items.some((item) => item.id === event.id)) return items;
          return [...items, event];
        });
      }, controller.signal)
      .catch((error) => {
        if (!controller.signal.aborted) setNotice(errorMessage(error));
      })
      .finally(() => setStreaming(false));
  }

  if (!token) {
    return (
      <main className="auth-shell">
        <section className="auth-card">
          <div className="brand-mark"><Bot size={30} /></div>
          <p className="eyebrow">Mghands Gateway</p>
          <h1>连接你的 OpenHands 沙箱工作台</h1>
          <p className="muted">登录后创建项目、启动隔离会话，并在同一界面跟踪代理事件流。</p>
          <form className="stack" onSubmit={handleLogin}>
            <label>
              用户名
              <input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" />
            </label>
            <label>
              密码
              <input value={password} onChange={(event) => setPassword(event.target.value)} type="password" autoComplete="current-password" />
            </label>
            <button className="primary" disabled={busy}>
              {busy ? <Loader2 className="spin" size={18} /> : <KeyRound size={18} />}
              登录
            </button>
          </form>
          {notice && <div className="notice danger">{notice}</div>}
        </section>
      </main>
    );
  }

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-head">
          <div className="brand-mark small"><Bot size={22} /></div>
          <div>
            <strong>Mghands</strong>
            <span>{user?.username || '已登录'}</span>
          </div>
          <button className="icon-button" onClick={logout} title="退出登录"><LogOut size={18} /></button>
        </div>

        <form className="project-form" onSubmit={createProject}>
          <input placeholder="新项目名称" value={projectName} onChange={(event) => setProjectName(event.target.value)} />
          <button disabled={busy || !projectName.trim()}><FolderPlus size={17} /></button>
        </form>

        <div className="skill-strip">
          <span><Sparkles size={15} /> 默认技能</span>
          <div className="skill-list">
            {skills.length ? skills.map((skill) => (
              <button
                key={skill.name}
                className={selectedSkillNames.includes(skill.name) ? 'chip selected' : 'chip'}
                onClick={() => setSelectedSkillNames((items) => items.includes(skill.name) ? items.filter((item) => item !== skill.name) : [...items, skill.name])}
                type="button"
              >
                {skill.name}
              </button>
            )) : <span className="muted">未配置共享技能</span>}
          </div>
          {defaultSkills.length > 0 && <small>系统默认: {defaultSkills.join(', ')}</small>}
        </div>

        <nav className="project-list">
          {projects.map((project) => (
            <button
              key={project.project_id}
              className={project.project_id === selectedProjectId ? 'project active' : 'project'}
              onClick={() => setSelectedProjectId(project.project_id)}
            >
              <span>{project.name}</span>
              <small>{new Date(project.updated_at).toLocaleString()}</small>
            </button>
          ))}
          {!projects.length && <div className="empty-mini">创建第一个项目开始使用</div>}
        </nav>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <p className="eyebrow">Project Workspace</p>
            <h1>{selectedProject?.name || '选择或创建项目'}</h1>
          </div>
          <div className="session-actions">
            <span className={`status ${session?.status || 'idle'}`}><RadioTower size={15} /> {statusLabel(session?.status)}</span>
            <button onClick={() => void ensureSession()} disabled={!selectedProject || busy}><Plus size={17} /> 新建会话</button>
            <button onClick={stopSession} disabled={!session || busy}><CircleStop size={17} /> 停止</button>
            <button onClick={() => session && startStream(session.session_id)} disabled={!session?.conversation_id || streaming}><RadioTower size={17} /> 监听</button>
          </div>
        </header>

        {notice && <div className="notice">{notice}</div>}

        <div className="content-grid">
          <section className="chat-panel">
            <div className="panel-title"><MessageSquareText size={18} /> 对话与任务</div>
            <div className="timeline">
              {events.map((event, index) => (
                <article className={`event-card ${String(event.kind || '').includes('error') ? 'error' : ''}`} key={`${event.id || 'local'}-${index}`}>
                  <div className="event-meta">
                    <strong>{eventTitle(event)}</strong>
                    <span>{event.timestamp ? new Date(event.timestamp).toLocaleTimeString() : ''}</span>
                  </div>
                  <pre>{eventPreview(event)}</pre>
                </article>
              ))}
              {!events.length && (
                <div className="empty-state">
                  <TerminalSquare size={42} />
                  <h2>还没有任务事件</h2>
                  <p>输入一个任务，Mghands 会创建沙箱会话并把 OpenHands 事件映射到这里。</p>
                </div>
              )}
            </div>
            <form className="composer" onSubmit={runPrompt}>
              <textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="例如: 检查当前工作区结构并运行测试" />
              <button className="primary" disabled={!selectedProject || busy || !prompt.trim()}>
                {busy ? <Loader2 className="spin" size={18} /> : <SendHorizontal size={18} />}
                发送
              </button>
            </form>
          </section>

          <aside className="inspector">
            <div className="panel-title"><Wrench size={18} /> 会话详情</div>
            <dl>
              <dt>Session ID</dt>
              <dd>{session?.session_id || '未创建'}</dd>
              <dt>Conversation</dt>
              <dd>{session?.conversation_id || '等待首次执行'}</dd>
              <dt>Sandbox</dt>
              <dd>{session?.sandbox_id || session?.sandbox_url || '未启动'}</dd>
              <dt>Last Event</dt>
              <dd>{session?.last_event_id || lastEventId || '无'}</dd>
            </dl>
            {session?.error && <div className="notice danger">{session.error}</div>}
            <div className="capability-card">
              <CheckCircle2 size={20} />
              <div>
                <strong>已适配 API</strong>
                <p>登录、项目、技能目录、会话、执行、历史与 SSE 流。</p>
              </div>
            </div>
          </aside>
        </div>
      </section>
    </main>
  );
}
