/**
 * Main App component — layout with viewport, chat panel, and debug panel.
 */

import { useEffect, useRef, useState } from 'react';
import Chat from './components/Chat';
import ProjectSettingsPanel from './components/ProjectSettingsPanel';
import BottomDock from './components/BottomDock';
import AppIcon from './components/AppIcon';
import HistorySidebar from './components/HistorySidebar';
import Viewport from './components/Viewport';
import { useWebSocket } from './hooks/useWebSocket';
import { useChatStore, useProjectStore, useSelectionStore, useViewportStore } from './stores';
import { api } from './api';
import { formatLocalDateTime } from './time';
import type { ChatMessage, ChatThread, ChatThreadSummary, ModelInfo, Project } from './types';
import { confirmDialog, promptDialog, DialogHost } from './components/ui/ConfirmDialog';
import { toast, ToastHost } from './components/ui/Toast';

function App() {
  const { project, setProject } = useProjectStore();
  const chat = useChatStore();
  const viewport = useViewportStore();
  const selection = useSelectionStore();
  const [projects, setProjects] = useState<Project[]>([]);
  const [chatThreads, setChatThreads] = useState<ChatThreadSummary[]>([]);
  const [modelVersions, setModelVersions] = useState<ModelInfo[]>([]);
  const [activeThreadId, setActiveThreadId] = useState<string | null>(null);
  const [initializing, setInitializing] = useState(true);
  const [constraintPanelOpen, setConstraintPanelOpen] = useState(false);
  const [historySidebarOpen, setHistorySidebarOpen] = useState(true);
  const [projectMenuOpen, setProjectMenuOpen] = useState(false);
  const [exportMenuOpen, setExportMenuOpen] = useState(false);
  const projectMenuRef = useRef<HTMLDivElement>(null);
  const exportMenuRef = useRef<HTMLDivElement>(null);
  const { sendMessage, sendRawMessage, isConnected } = useWebSocket(project?.project_id ?? null, activeThreadId);

  // Close popovers on outside-click / Escape
  useEffect(() => {
    function handleDocClick(e: MouseEvent) {
      if (projectMenuOpen && projectMenuRef.current && !projectMenuRef.current.contains(e.target as Node)) {
        setProjectMenuOpen(false);
      }
      if (exportMenuOpen && exportMenuRef.current && !exportMenuRef.current.contains(e.target as Node)) {
        setExportMenuOpen(false);
      }
    }
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        setProjectMenuOpen(false);
        setExportMenuOpen(false);
      }
    }
    document.addEventListener('mousedown', handleDocClick);
    document.addEventListener('keydown', handleKey);
    return () => {
      document.removeEventListener('mousedown', handleDocClick);
      document.removeEventListener('keydown', handleKey);
    };
  }, [projectMenuOpen, exportMenuOpen]);

  // On mount: create or load a project
  useEffect(() => {
    initProject();
  }, []);

  async function initProject() {
    try {
      // Try to load existing projects
      const projects = await api.get<Project[]>('/api/projects');
      setProjects(projects);
      if (projects.length > 0) {
        setProject(projects[0]);
      } else {
        // Create a new project
        const newProject = await api.post<Project>('/api/projects', {
          name: 'My First Project',
        });
        setProjects([newProject]);
        setProject(newProject);
      }
    } catch (err) {
      console.error('Failed to initialize project:', err);
    } finally {
      setInitializing(false);
    }
  }

  useEffect(() => {
    if (!project) return;
    loadChatThreads(project.project_id);
    loadModelVersions(project.project_id);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project?.project_id]);

  useEffect(() => {
    async function handleModelReady(event: Event) {
      const detail = (event as CustomEvent<{ projectId: string; modelId: string }>).detail;
      if (!project || detail.projectId !== project.project_id) return;
      
      // Refresh project to get new updated_at timestamp
      try {
        const updated = await api.get<Project>(`/api/projects/${project.project_id}`);
        setProject(updated);
        setProjects(current => current.map(p => p.project_id === updated.project_id ? updated : p));
      } catch (err) {
        console.error('Failed to refresh project after model ready:', err);
      }

      loadModelVersions(project.project_id, detail.modelId);
    }

    window.addEventListener('cad-model-ready', handleModelReady);
    return () => window.removeEventListener('cad-model-ready', handleModelReady);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project?.project_id]);

  async function loadChatThreads(projectId: string) {
    try {
      const threads = await api.get<ChatThreadSummary[]>(`/api/projects/${projectId}/chat_threads`);
      if (threads.length === 0) {
        const thread = await api.post<ChatThread>(`/api/projects/${projectId}/chat_threads`, {
          title: 'New chat',
        });
        const summary = toThreadSummary(thread);
        setChatThreads([summary]);
        setActiveThreadId(thread.thread_id);
        loadMessages(projectId, thread.thread_id);
        return;
      }

      setChatThreads(threads);
      const nextThreadId = threads[0].thread_id;
      setActiveThreadId(nextThreadId);
      loadMessages(projectId, nextThreadId);
    } catch (err) {
      console.error('Failed to load chat threads:', err);
      setChatThreads([]);
      setActiveThreadId(null);
      chat.reset();
    }
  }

  async function loadMessages(projectId: string, threadId: string) {
    try {
      const messages = await api.get<ChatMessage[]>(
        `/api/projects/${projectId}/history?thread_id=${encodeURIComponent(threadId)}`
      );
      chat.setMessages(messages);
      chat.clearStream();
      chat.setGenerating(false);
    } catch (err) {
      console.error('Failed to load messages:', err);
      chat.reset();
    }
  }

  async function loadModelVersions(projectId: string, preferredModelId?: string) {
    try {
      const models = await api.get<ModelInfo[]>(`/api/projects/${projectId}/models`);
      const successful = models.filter((model) => model.has_glb);
      setModelVersions(successful);

      const selected =
        successful.find((model) => model.model_id === preferredModelId) ??
        successful.at(-1);

      if (selected) {
        viewport.setModel(
          selected.model_id,
          api.url(`/api/projects/${projectId}/models/${selected.model_id}/glb`),
          projectId
        );
      } else {
        viewport.reset();
        viewport.setProjectId(projectId);
      }
    } catch (err) {
      console.error('Failed to load model versions:', err);
      setModelVersions([]);
      viewport.reset();
    }
  }

  function handleModelVersionChange(modelId: string) {
    if (!project || !modelId) return;
    viewport.setModel(
      modelId,
      api.url(`/api/projects/${project.project_id}/models/${modelId}/glb`),
      project.project_id
    );
  }

  async function handleProjectChange(projectId: string) {
    const nextProject = projects.find((p) => p.project_id === projectId);
    if (!nextProject || nextProject.project_id === project?.project_id) return;
    setProject(nextProject);
    setChatThreads([]);
    setModelVersions([]);
    setActiveThreadId(null);
    chat.reset();
    viewport.reset();
  }

  async function handleNewProject() {
    const name = `Untitled Project ${projects.length + 1}`;
    const newProject = await api.post<Project>('/api/projects', { name });
    setProjects((current) => [newProject, ...current]);
    setProject(newProject);
    setChatThreads([]);
    setModelVersions([]);
    setActiveThreadId(null);
    chat.reset();
    viewport.reset();
  }

  async function handleRenameProject(newName: string) {
    if (!project) return;
    const name = newName.trim();
    if (!name || name === project.name) return;

    const updated = await api.put<Project>(`/api/projects/${project.project_id}`, { name });
    setProject(updated);
    setProjects((current) =>
      current.map((item) => (item.project_id === updated.project_id ? updated : item))
    );
  }

  async function handleDeleteProject() {
    if (!project) return;
    const confirmed = await confirmDialog({
      title: 'Delete project?',
      message: `Delete project "${project.name}"? This removes its chats and models.`,
      confirmLabel: 'Delete project',
      tone: 'danger',
    });
    if (!confirmed) return;

    await api.delete(`/api/projects/${project.project_id}`);
    const remaining = projects.filter((item) => item.project_id !== project.project_id);

    if (remaining.length > 0) {
      setProjects(remaining);
      setProject(remaining[0]);
    } else {
      const replacement = await api.post<Project>('/api/projects', { name: 'Untitled Project 1' });
      setProjects([replacement]);
      setProject(replacement);
    }

    setChatThreads([]);
    setModelVersions([]);
    setActiveThreadId(null);
    chat.reset();
    viewport.reset();
  }

  async function handleOpenProjectFolder() {
    if (!project) return;
    try {
      await api.post(`/api/projects/${project.project_id}/open_folder`);
    } catch (err) {
      console.error('Failed to open project folder:', err);
      toast.error(`Could not open project folder: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  async function handleExport(format: 'step' | 'stl' | 'glb' | 'source') {
    if (!project || !viewport.currentModelId) return;

    try {
      await api.downloadFile(
        `/api/projects/${project.project_id}/models/${viewport.currentModelId}/${format}`,
        `model_${viewport.currentModelId}.${format === 'source' ? 'py' : format}`
      );
      toast.success(`Exported ${format.toUpperCase()}`);
    } catch (err) {
      toast.error(`Failed to export ${format.toUpperCase()}: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setExportMenuOpen(false);
    }
  }

  async function handleThreadChange(threadId: string) {
    if (!project || threadId === activeThreadId) return;
    setActiveThreadId(threadId);
    await loadMessages(project.project_id, threadId);
  }

  async function handleNewThread() {
    if (!project) return;
    const thread = await api.post<ChatThread>(`/api/projects/${project.project_id}/chat_threads`, {
      title: 'New chat',
    });
    setChatThreads((current) => [toThreadSummary(thread), ...current]);
    setActiveThreadId(thread.thread_id);
    chat.reset();
    viewport.reset();
  }

  async function handleRenameThread(threadId: string, nextTitle?: string) {
    if (!project) return;
    const current = chatThreads.find((thread) => thread.thread_id === threadId);
    let title = nextTitle?.trim();
    if (!title) {
      const result = await promptDialog({
        title: 'Rename chat',
        initialValue: current?.title ?? 'New chat',
        placeholder: 'Chat title',
        confirmLabel: 'Rename',
      });
      title = result?.trim();
    }
    if (!title || title === current?.title) return;

    const updated = await api.put<ChatThread>(
      `/api/projects/${project.project_id}/chat_threads/${threadId}`,
      { title }
    );
    setChatThreads((threads) =>
      threads.map((thread) =>
        thread.thread_id === threadId
          ? { ...thread, title: updated.title, updated_at: updated.updated_at }
          : thread
      )
    );
    toast.success('Chat renamed');
  }

  async function handleDeleteThread(threadId: string) {
    if (!project) return;
    const current = chatThreads.find((thread) => thread.thread_id === threadId);
    const confirmed = await confirmDialog({
      title: 'Delete chat?',
      message: `Delete chat "${current?.title ?? 'this chat'}"?`,
      confirmLabel: 'Delete chat',
      tone: 'danger',
    });
    if (!confirmed) return;

    await api.delete(`/api/projects/${project.project_id}/chat_threads/${threadId}`);
    await loadChatThreads(project.project_id);
    if (threadId === activeThreadId) {
      chat.reset();
      viewport.reset();
    }
    toast.success('Chat deleted');
  }

  function handleSend(message: string) {
    if (!activeThreadId) return;
    setChatThreads((current) =>
      current.map((thread) =>
        thread.thread_id === activeThreadId
          ? {
              ...thread,
              title: thread.title === 'New chat' ? compactTitle(message) : thread.title,
              message_count: thread.message_count + 1,
              updated_at: new Date().toISOString(),
            }
          : thread
      )
    );
    sendMessage(message);
  }

  function compactTitle(message: string): string {
    const title = message.trim().replace(/\s+/g, ' ');
    return title.length > 48 ? `${title.slice(0, 48)}...` : title;
  }

  function toThreadSummary(thread: ChatThread): ChatThreadSummary {
    const lastMessage = thread.messages.at(-1) ?? null;
    return {
      thread_id: thread.thread_id,
      title: thread.title,
      created_at: thread.created_at,
      updated_at: thread.updated_at,
      message_count: thread.messages.length,
      last_message: lastMessage,
    };
  }

  if (initializing) {
    return (
      <div className="app-loading">
        <div className="app-loading-spinner" />
        <p>Connecting to Mission Crafter...</p>
      </div>
    );
  }

  if (!project) {
    return (
      <div className="app-loading">
        <p className="app-error">
          ❌ Could not connect to backend. Make sure the server is running on port 8000.
        </p>
      </div>
    );
  }

  return (
    <div className="app">
      {/* Header */}
      <header className="app-header">
        <div className="app-header-brand">
          <span className="app-logo" aria-hidden="true">
            <AppIcon size={22} />
          </span>
          <h1>Mission Crafter</h1>
        </div>

        <div className="app-header-project">
          <div className="project-dropdown" ref={projectMenuRef}>
            <button
              className="project-dropdown-btn"
              type="button"
              aria-haspopup="menu"
              aria-expanded={projectMenuOpen}
              onClick={() => setProjectMenuOpen((v) => !v)}
            >
              <div className="project-dropdown-name">
                {project.name}
                <span className="project-dropdown-caret" aria-hidden="true">▾</span>
              </div>
              <div className="project-dropdown-time">
                Saved {formatLocalDateTime(project.updated_at)}
              </div>
            </button>
            {projectMenuOpen && (
              <div className="project-dropdown-menu" role="menu">
                {projects.map((p) => (
                  <button
                    key={p.project_id}
                    role="menuitem"
                    className={p.project_id === project.project_id ? 'active' : ''}
                    onClick={() => { setProjectMenuOpen(false); handleProjectChange(p.project_id); }}
                    type="button"
                  >
                    <div className="project-dropdown-name">{p.name}</div>
                    <div className="project-dropdown-time">
                      Saved {formatLocalDateTime(p.updated_at)}
                    </div>
                  </button>
                ))}
                <div className="project-dropdown-divider" />
                <button
                  role="menuitem"
                  onClick={() => { setProjectMenuOpen(false); handleNewProject(); }}
                  type="button"
                >
                  <div className="project-dropdown-name">＋ New project</div>
                </button>
              </div>
            )}
          </div>

          <button
            className="btn btn-ghost"
            type="button"
            onClick={() => setConstraintPanelOpen(true)}
            title="Edit project settings and constraints"
          >
            Project settings
          </button>
        </div>

        <div className="app-header-actions">
          <div className="export-dropdown" ref={exportMenuRef}>
            <button
              className="btn btn-ghost"
              type="button"
              aria-haspopup="menu"
              aria-expanded={exportMenuOpen}
              disabled={!viewport.currentModelId}
              title={viewport.currentModelId ? 'Download model files' : 'Generate a model first'}
              onClick={() => setExportMenuOpen((v) => !v)}
            >
              Export <span aria-hidden="true">▾</span>
            </button>
            {exportMenuOpen && viewport.currentModelId && (
              <div className="export-menu" role="menu">
                <button role="menuitem" onClick={() => handleExport('step')}>
                  Download STEP (.step)
                </button>
                <button role="menuitem" onClick={() => handleExport('stl')}>
                  Download STL (.stl)
                </button>
                <button role="menuitem" onClick={() => handleExport('glb')}>
                  Download GLB (.glb)
                </button>
                <button role="menuitem" onClick={() => handleExport('source')}>
                  Download source (.py)
                </button>
              </div>
            )}
          </div>

          <div
            className={`connection-status ${isConnected ? 'is-connected' : 'is-disconnected'}`}
            title={isConnected ? 'Realtime connection live' : 'Reconnecting to server…'}
          >
            <span className={`connection-dot ${isConnected ? 'connected' : 'disconnected'}`} />
            <span>{isConnected ? 'Connected' : 'Reconnecting…'}</span>
          </div>
        </div>
      </header>

      {/* Main layout */}
      <div className="app-main">
        {historySidebarOpen && (
          <HistorySidebar
            versions={modelVersions}
            onSelectModel={handleModelVersionChange}
            threads={chatThreads}
            activeThreadId={activeThreadId}
            onSelectThread={handleThreadChange}
            onNewThread={handleNewThread}
            onRenameThread={handleRenameThread}
            onDeleteThread={handleDeleteThread}
          />
        )}

        {/* 3D Viewport */}
        <div className="app-viewport">
          <button
            className="viewport-history-toggle"
            onClick={() => setHistorySidebarOpen(!historySidebarOpen)}
            title={historySidebarOpen ? 'Hide sidebar' : 'Show sidebar'}
            aria-label={historySidebarOpen ? 'Hide sidebar' : 'Show sidebar'}
          >
            {historySidebarOpen ? '◀' : '▶'}
          </button>

          {!historySidebarOpen && (
            <div className="model-version-bar">
              <span className="model-version-label">Model version</span>
              <select
                className="model-version-select"
                value={viewport.currentModelId ?? ''}
                onChange={(e) => handleModelVersionChange(e.target.value)}
                disabled={modelVersions.length === 0}
                aria-label="Select model version"
              >
                {modelVersions.length === 0 ? (
                  <option value="">No versions yet</option>
                ) : (
                  modelVersions.map((model) => (
                    <option key={model.model_id} value={model.model_id}>
                      #{model.model_id.slice(-6)} — {formatLocalDateTime(model.created_at)} — {model.prompt || 'checkpoint'}
                    </option>
                  ))
                )}
              </select>
            </div>
          )}
          <Viewport
            onSelect={(cadName, point) => {
              selection.setSelection(cadName, point ?? undefined);
            }}
            sendWsMessage={sendRawMessage}
          />
        </div>

        {/* Chat Panel */}
        <div className="app-chat">
          <Chat onSend={handleSend} disabled={!activeThreadId || !isConnected} />
        </div>
      </div>

      {/* Unified bottom dock — Source / Assembly / Features / Parameters / Debug */}
      <BottomDock />

      <ProjectSettingsPanel
        isOpen={constraintPanelOpen}
        onClose={() => setConstraintPanelOpen(false)}
        onRenameProject={handleRenameProject}
        onDeleteProject={handleDeleteProject}
        onOpenProjectFolder={handleOpenProjectFolder}
      />

      <DialogHost />
      <ToastHost />
    </div>
  );
}

export default App;
