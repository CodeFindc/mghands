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
  Users,
  Cpu,
  Trash2,
  ArrowLeft,
  Settings,
  PlusCircle,
  RefreshCw,
} from 'lucide-react';
import { ApiError, api, errorMessage } from './api';
import type { Project, Session, SkillCatalogItem, TimelineEvent, User, LLMModel, SystemSettings } from './types';

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

  // Admin View States
  const [isAdminView, setIsAdminView] = useState(false);
  const [adminTab, setAdminTab] = useState<'users' | 'resources' | 'skills' | 'models'>('users');
  const [adminUsers, setAdminUsers] = useState<User[]>([]);
  const [adminSettings, setAdminSettings] = useState<SystemSettings>({});
  const [adminSkills, setAdminSkills] = useState<SkillCatalogItem[]>([]);
  const [adminModels, setAdminModels] = useState<LLMModel[]>([]);

  // User tab form states
  const [newUsername, setNewUsername] = useState('');
  const [newUserPassword, setNewUserPassword] = useState('');
  const [newUserRole, setNewUserRole] = useState<'admin' | 'user'>('user');
  const [resetPassUserId, setResetPassUserId] = useState<string | null>(null);
  const [resetPassValue, setResetPassValue] = useState('');

  // Settings tab form states
  const [settingsImage, setSettingsImage] = useState('');
  const [settingsMemory, setSettingsMemory] = useState('');
  const [settingsCpus, setSettingsCpus] = useState('');
  const [settingsPids, setSettingsPids] = useState('');

  // Skills tab form states
  const [uploadSkillName, setUploadSkillName] = useState('');
  const [uploadSkillFile, setUploadSkillFile] = useState<File | null>(null);

  // Models tab form states
  const [editingModel, setEditingModel] = useState<Partial<LLMModel> | null>(null);
  const [modelName, setModelName] = useState('');
  const [modelProvider, setModelProvider] = useState('');
  const [modelModel, setModelModel] = useState('');
  const [modelBaseUrl, setModelBaseUrl] = useState('');
  const [modelApiKey, setModelApiKey] = useState('');
  const [modelIsDefault, setModelIsDefault] = useState(false);

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
    if (!token || !selectedProjectId || isAdminView) return;
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
  }, [selectedProjectId, sessionMap, token, isAdminView]);

  // Load admin tab data dynamically
  async function loadAdminData(tab = adminTab) {
    if (!token) return;
    setBusy(true);
    try {
      if (tab === 'users') {
        const list = await api.adminListUsers(token);
        setAdminUsers(list);
      } else if (tab === 'resources') {
        const data = await api.adminGetSettings(token);
        setAdminSettings(data);
      } else if (tab === 'skills') {
        const data = await api.adminListSkills(token);
        setAdminSkills(data);
      } else if (tab === 'models') {
        const list = await api.adminListModels(token);
        setAdminModels(list);
      }
      setNotice(null);
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (isAdminView) {
      void loadAdminData(adminTab);
    }
  }, [isAdminView, adminTab]);

  // Prefill settings form
  useEffect(() => {
    if (adminTab === 'resources' && adminSettings) {
      setSettingsImage(adminSettings.sandbox_image || '');
      setSettingsMemory(adminSettings.sandbox_memory_limit || '');
      setSettingsCpus(adminSettings.sandbox_cpus || '');
      setSettingsPids(adminSettings.sandbox_pids_limit || '');
    }
  }, [adminSettings, adminTab]);

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
    setIsAdminView(false);
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

  // Admin Tab Action Handlers
  async function handleCreateUser(e: FormEvent) {
    e.preventDefault();
    if (!token || !newUsername.trim() || !newUserPassword.trim()) return;
    setBusy(true);
    try {
      await api.adminCreateUser(token, {
        username: newUsername.trim(),
        password: newUserPassword.trim(),
        role: newUserRole,
        enabled: true,
      });
      setNewUsername('');
      setNewUserPassword('');
      setNewUserRole('user');
      setNotice('用户已创建');
      await loadAdminData('users');
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function handleToggleUserEnabled(userId: string, currentEnabled: boolean) {
    if (!token) return;
    setBusy(true);
    try {
      await api.adminUpdateUser(token, userId, { enabled: !currentEnabled });
      setNotice('用户状态已更新');
      await loadAdminData('users');
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function handleToggleUserRole(userId: string, currentRole: 'admin' | 'user') {
    if (!token) return;
    setBusy(true);
    try {
      await api.adminUpdateUser(token, userId, { role: currentRole === 'admin' ? 'user' : 'admin' });
      setNotice('用户角色已更新');
      await loadAdminData('users');
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function handleResetPassword(e: FormEvent) {
    e.preventDefault();
    if (!token || !resetPassUserId || !resetPassValue.trim()) return;
    setBusy(true);
    try {
      await api.adminResetPassword(token, resetPassUserId, resetPassValue.trim());
      setResetPassUserId(null);
      setResetPassValue('');
      setNotice('密码已成功重置');
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function handleSaveSettings(e: FormEvent) {
    e.preventDefault();
    if (!token) return;
    setBusy(true);
    try {
      await api.adminSaveSettings(token, {
        sandbox_image: settingsImage.trim(),
        sandbox_memory_limit: settingsMemory.trim(),
        sandbox_cpus: settingsCpus.trim(),
        sandbox_pids_limit: settingsPids.trim(),
      });
      setNotice('资源限制参数已保存');
      await loadAdminData('resources');
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function handleUploadSkill(e: FormEvent) {
    e.preventDefault();
    if (!token || !uploadSkillName.trim() || !uploadSkillFile) return;
    setBusy(true);
    try {
      await api.adminUploadSkill(token, uploadSkillName.trim(), uploadSkillFile);
      setUploadSkillName('');
      setUploadSkillFile(null);
      const fileInput = document.getElementById('skill-file-input') as HTMLInputElement;
      if (fileInput) fileInput.value = '';
      setNotice('共享技能已成功上传并就绪');
      await loadAdminData('skills');
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function handleDeleteSkill(skillName: string) {
    if (!token || !confirm(`确定要删除共享技能 "${skillName}" 吗？`)) return;
    setBusy(true);
    try {
      await api.adminDeleteSkill(token, skillName);
      setNotice('技能已从共享仓库中删除');
      await loadAdminData('skills');
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function handleSaveModel(e: FormEvent) {
    e.preventDefault();
    if (!token || !modelName.trim() || !modelProvider.trim() || !modelModel.trim()) return;
    setBusy(true);
    try {
      const payload: any = {
        name: modelName.trim(),
        provider: modelProvider.trim(),
        model: modelModel.trim(),
        base_url: modelBaseUrl.trim() || null,
        is_default: modelIsDefault,
      };
      if (modelApiKey.trim()) {
        payload.api_key = modelApiKey.trim();
      }

      if (editingModel && editingModel.model_id) {
        await api.adminUpdateModel(token, editingModel.model_id, payload);
        setNotice('模型集成配置已更新');
      } else {
        await api.adminCreateModel(token, payload);
        setNotice('已成功添加新模型接入配置');
      }

      setEditingModel(null);
      setModelName('');
      setModelProvider('');
      setModelModel('');
      setModelBaseUrl('');
      setModelApiKey('');
      setModelIsDefault(false);
      await loadAdminData('models');
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function handleDeleteModel(modelId: string) {
    if (!token || !confirm('确定要删除此模型配置吗？')) return;
    setBusy(true);
    try {
      await api.adminDeleteModel(token, modelId);
      setNotice('模型配置已删除');
      await loadAdminData('models');
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  function startEditModel(model: LLMModel) {
    setEditingModel(model);
    setModelName(model.name);
    setModelProvider(model.provider);
    setModelModel(model.model);
    setModelBaseUrl(model.base_url || '');
    setModelApiKey('');
    setModelIsDefault(model.is_default);
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

        {isAdminView ? (
          <>
            <div className="sidebar-title">系统管理</div>
            <nav className="project-list">
              <button className={adminTab === 'users' ? 'project active' : 'project'} onClick={() => setAdminTab('users')}>
                <Users size={16} /> <span>用户管理</span>
              </button>
              <button className={adminTab === 'resources' ? 'project active' : 'project'} onClick={() => setAdminTab('resources')}>
                <Cpu size={16} /> <span>资源配置</span>
              </button>
              <button className={adminTab === 'skills' ? 'project active' : 'project'} onClick={() => setAdminTab('skills')}>
                <Sparkles size={16} /> <span>技能仓库</span>
              </button>
              <button className={adminTab === 'models' ? 'project active' : 'project'} onClick={() => setAdminTab('models')}>
                <Settings size={16} /> <span>模型集成</span>
              </button>
            </nav>
            <div className="sidebar-footer" style={{ marginTop: 'auto', width: '100%' }}>
              <button className="primary-back-btn" onClick={() => setIsAdminView(false)}>
                <ArrowLeft size={16} /> 返回工作区
              </button>
            </div>
          </>
        ) : (
          <>
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

            {user?.role === 'admin' && (
              <div className="sidebar-footer" style={{ marginTop: 'auto', width: '100%' }}>
                <button className="primary-admin-btn" onClick={() => setIsAdminView(true)}>
                  <Settings size={16} /> 系统管理面板
                </button>
              </div>
            )}
          </>
        )}
      </aside>

      {isAdminView ? (
        <section className="workspace">
          <header className="topbar">
            <div>
              <p className="eyebrow">System Administration</p>
              <h1>
                {adminTab === 'users'
                  ? '用户账户管理'
                  : adminTab === 'resources'
                  ? '沙箱资源限制配置'
                  : adminTab === 'skills'
                  ? '共享技能仓库'
                  : '接入模型配置'}
              </h1>
            </div>
          </header>

          {notice && <div className="notice">{notice}</div>}

          <div className="admin-content-shell">
            {adminTab === 'users' && (
              <div className="admin-grid">
                <div className="admin-card">
                  <div className="panel-title"><Users size={18} /> 用户列表</div>
                  <div className="admin-table-container">
                    <table className="admin-table">
                      <thead>
                        <tr>
                          <th>用户名</th>
                          <th>角色</th>
                          <th>状态</th>
                          <th>操作</th>
                        </tr>
                      </thead>
                      <tbody>
                        {adminUsers.map((u) => (
                          <tr key={u.user_id}>
                            <td>{u.username}</td>
                            <td>
                              <button
                                className={`role-badge ${u.role}`}
                                onClick={() => handleToggleUserRole(u.user_id, u.role)}
                                disabled={busy || u.user_id === user?.user_id}
                                title="点击切换角色"
                              >
                                {u.role === 'admin' ? '管理员' : '普通用户'}
                              </button>
                            </td>
                            <td>
                              <button
                                className={`status-badge ${u.enabled ? 'active' : 'disabled'}`}
                                onClick={() => handleToggleUserEnabled(u.user_id, u.enabled)}
                                disabled={busy || u.user_id === user?.user_id}
                                title="点击启用/禁用"
                              >
                                {u.enabled ? '启用中' : '已禁用'}
                              </button>
                            </td>
                            <td>
                              <button
                                className="text-action-btn"
                                onClick={() => setResetPassUserId(u.user_id)}
                              >
                                重置密码
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>

                <div className="admin-sidebar-forms">
                  <div className="admin-card">
                    <div className="panel-title"><PlusCircle size={18} /> 创建新用户</div>
                    <form className="stack" onSubmit={handleCreateUser}>
                      <label>
                        用户名
                        <input value={newUsername} onChange={(e) => setNewUsername(e.target.value)} placeholder="输入登录名" required />
                      </label>
                      <label>
                        密码
                        <input value={newUserPassword} onChange={(e) => setNewUserPassword(e.target.value)} type="password" placeholder="输入密码（至少8位）" required />
                      </label>
                      <label>
                        角色
                        <select className="premium-select" value={newUserRole} onChange={(e) => setNewUserRole(e.target.value as 'admin' | 'user')}>
                          <option value="user">普通用户</option>
                          <option value="admin">系统管理员</option>
                        </select>
                      </label>
                      <button className="primary" disabled={busy}>创建用户</button>
                    </form>
                  </div>

                  {resetPassUserId && (
                    <div className="admin-card alert-card">
                      <div className="panel-title">重置用户密码</div>
                      <form className="stack" onSubmit={handleResetPassword}>
                        <label>
                          新密码
                          <input value={resetPassValue} onChange={(e) => setResetPassValue(e.target.value)} type="password" placeholder="输入新密码" required />
                        </label>
                        <div className="btn-group">
                          <button className="primary danger-btn" disabled={busy}>确认重置</button>
                          <button className="secondary-btn" type="button" onClick={() => setResetPassUserId(null)}>取消</button>
                        </div>
                      </form>
                    </div>
                  )}
                </div>
              </div>
            )}

            {adminTab === 'resources' && (
              <div className="admin-card max-width-card">
                <div className="panel-title"><Cpu size={18} /> 容器沙箱物理资源配额</div>
                <p className="muted">管理会话按需创建 Docker 容器时的物理资源上限限制与默认基础镜像配置。</p>
                <form className="stack" onSubmit={handleSaveSettings}>
                  <label>
                    默认基础镜像 (sandbox_image)
                    <input value={settingsImage} onChange={(e) => setSettingsImage(e.target.value)} placeholder="例如: mghands-sandbox:latest" required />
                  </label>
                  <label>
                    内存上限限制 (sandbox_memory_limit)
                    <input value={settingsMemory} onChange={(e) => setSettingsMemory(e.target.value)} placeholder="例如: 2g, 4g, 512m" required />
                  </label>
                  <label>
                    CPU 核心限制 (sandbox_cpus)
                    <input value={settingsCpus} onChange={(e) => setSettingsCpus(e.target.value)} placeholder="例如: 2, 4" required />
                  </label>
                  <label>
                    进程数并发上限 (sandbox_pids_limit)
                    <input value={settingsPids} onChange={(e) => setSettingsPids(e.target.value)} type="number" placeholder="例如: 512" required />
                  </label>
                  <button className="primary" disabled={busy}>保存修改</button>
                </form>
              </div>
            )}

            {adminTab === 'skills' && (
              <div className="admin-grid">
                <div className="admin-card">
                  <div className="panel-title"><Sparkles size={18} /> 共享技能列表</div>
                  <div className="admin-table-container">
                    <table className="admin-table">
                      <thead>
                        <tr>
                          <th>技能名称</th>
                          <th>触发词/Triggers</th>
                          <th>第三方依赖</th>
                          <th>操作</th>
                        </tr>
                      </thead>
                      <tbody>
                        {adminSkills.map((s) => (
                          <tr key={s.name}>
                            <td>
                              <strong>{s.name}</strong>
                              {s.metadata?.description && <p className="table-desc">{s.metadata.description}</p>}
                            </td>
                            <td>
                              {s.metadata?.triggers?.length ? s.metadata.triggers.map(t => (
                                <span className="chip mini" key={t}>{t}</span>
                              )) : <span className="muted text-mini">无</span>}
                            </td>
                            <td>
                              {s.metadata?.requires_dependencies ? (
                                <span className="chip mini warning" title={s.metadata.dependencies?.join('\n')}>
                                  {s.metadata.dependencies?.length} 个依赖
                                </span>
                              ) : <span className="muted text-mini">无</span>}
                            </td>
                            <td>
                              <button
                                className="text-action-btn danger"
                                onClick={() => handleDeleteSkill(s.name)}
                                disabled={busy}
                              >
                                <Trash2 size={15} /> 删除
                              </button>
                            </td>
                          </tr>
                        ))}
                        {!adminSkills.length && (
                          <tr>
                            <td colSpan={4} className="empty-table-row">暂未上传任何共享技能</td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                </div>

                <div className="admin-sidebar-forms">
                  <div className="admin-card">
                    <div className="panel-title">发布共享技能 ZIP</div>
                    <p className="muted">上传打包好的技能文件夹 ZIP 压缩包，技能根目录下必须包含 `SKILL.md`。</p>
                    <form className="stack" onSubmit={handleUploadSkill}>
                      <label>
                        技能唯一安全标识
                        <input value={uploadSkillName} onChange={(e) => setUploadSkillName(e.target.value)} placeholder="英文/数字，如: my_git_helper" required />
                      </label>
                      <label>
                        选择 ZIP 文件
                        <input
                          id="skill-file-input"
                          type="file"
                          accept=".zip"
                          onChange={(e) => setUploadSkillFile(e.target.files?.[0] || null)}
                          required
                        />
                      </label>
                      <button className="primary" disabled={busy}>上传并解压发布</button>
                    </form>
                  </div>
                </div>
              </div>
            )}

            {adminTab === 'models' && (
              <div className="admin-grid">
                <div className="admin-card">
                  <div className="panel-title"><Settings size={18} /> 模型配置列表</div>
                  <div className="admin-table-container">
                    <table className="admin-table">
                      <thead>
                        <tr>
                          <th>显示名称</th>
                          <th>接入提供商</th>
                          <th>模型标识</th>
                          <th>状态</th>
                          <th>操作</th>
                        </tr>
                      </thead>
                      <tbody>
                        {adminModels.map((m) => (
                          <tr key={m.model_id}>
                            <td>
                              <strong>{m.name}</strong>
                              {m.base_url && <p className="table-desc">{m.base_url}</p>}
                            </td>
                            <td><span className="chip mini">{m.provider}</span></td>
                            <td><code>{m.model}</code></td>
                            <td>
                              {m.is_default ? (
                                <span className="chip mini default-badge">系统默认</span>
                              ) : <span className="muted text-mini">-</span>}
                            </td>
                            <td>
                              <div className="row-action-group">
                                <button className="text-action-btn" onClick={() => startEditModel(m)}>编辑</button>
                                <button className="text-action-btn danger" onClick={() => handleDeleteModel(m.model_id)}>删除</button>
                              </div>
                            </td>
                          </tr>
                        ))}
                        {!adminModels.length && (
                          <tr>
                            <td colSpan={5} className="empty-table-row">未配置任何大语言模型接入</td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                </div>

                <div className="admin-sidebar-forms">
                  <div className="admin-card">
                    <div className="panel-title">
                      {editingModel ? '修改模型接入' : '新增接入模型'}
                    </div>
                    <form className="stack" onSubmit={handleSaveModel}>
                      <label>
                        显示名称
                        <input value={modelName} onChange={(e) => setModelName(e.target.value)} placeholder="如: Ollama-Llama3" required />
                      </label>
                      <label>
                        大模型提供商 (Provider)
                        <input value={modelProvider} onChange={(e) => setModelProvider(e.target.value)} placeholder="openai, anthropic, ollama, ollama/..., custom" required />
                      </label>
                      <label>
                        模型具体标识 (Model ID)
                        <input value={modelModel} onChange={(e) => setModelModel(e.target.value)} placeholder="如: gpt-4o, llama3" required />
                      </label>
                      <label>
                        自定义 Endpoint URL (Base URL)
                        <input value={modelBaseUrl} onChange={(e) => setModelBaseUrl(e.target.value)} placeholder="http://127.0.0.1:11434" />
                      </label>
                      <label>
                        API Key (密钥)
                        <input value={modelApiKey} onChange={(e) => setModelApiKey(e.target.value)} type="password" placeholder={editingModel ? '留空表示不修改已有密钥' : '根据模型供应商提供，无需则留空'} />
                      </label>
                      <label className="checkbox-label">
                        <input type="checkbox" checked={modelIsDefault} onChange={(e) => setModelIsDefault(e.target.checked)} />
                        设为系统全局默认模型
                      </label>
                      <div className="btn-group">
                        <button className="primary" disabled={busy}>保存配置</button>
                        {editingModel && (
                          <button
                            className="secondary-btn"
                            type="button"
                            onClick={() => {
                              setEditingModel(null);
                              setModelName('');
                              setModelProvider('');
                              setModelModel('');
                              setModelBaseUrl('');
                              setModelApiKey('');
                              setModelIsDefault(false);
                            }}
                          >
                            取消
                          </button>
                        )}
                      </div>
                    </form>
                  </div>
                </div>
              </div>
            )}
          </div>
        </section>
      ) : (
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
      )}
    </main>
  );
}
