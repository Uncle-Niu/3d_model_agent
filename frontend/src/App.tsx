/**
 * Main App component — layout with viewport, chat panel, and debug panel.
 */

import { useEffect, useState } from 'react';
import Chat from './components/Chat';
import ProjectSettingsPanel from './components/ProjectSettingsPanel';
import DebugPanel from './components/DebugPanel';
import HistorySidebar from './components/HistorySidebar';
import SourcePanel from './components/SourcePanel';
import Viewport from './components/Viewport';
import ParameterPanel from './components/ParameterPanel';
import FeaturePanel from './components/FeaturePanel';
import AssemblyPanel from './components/AssemblyPanel';
import { useWebSocket } from './hooks/useWebSocket';
import { useChatStore, useProjectStore, useSelectionStore, useViewportStore } from './stores';
import { api } from './api';
import { formatLocalDateTime } from './time';
import type { ChatMessage, ChatThread, ChatThreadSummary, ModelInfo, Project } from './types';

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
  const { sendMessage, sendRawMessage, isConnected } = useWebSocket(project?.project_id ?? null, activeThreadId);

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
    function handleModelReady(event: Event) {
      const detail = (event as CustomEvent<{ projectId: string; modelId: string }>).detail;
      if (!project || detail.projectId !== project.project_id) return;
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
    const confirmed = window.confirm(`Delete project "${project.name}"? This removes its chats and models.`);
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
      alert(`Could not open project folder: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  async function handleExport(format: 'step' | 'stl' | 'glb' | 'source') {
    if (!project || !viewport.currentModelId) return;
    
    try {
      await api.downloadFile(
        `/api/projects/${project.project_id}/models/${viewport.currentModelId}/${format}`,
        `model_${viewport.currentModelId}.${format === 'source' ? 'py' : format}`
      );
    } catch (err) {
      alert(`Failed to export ${format.toUpperCase()}: ${err instanceof Error ? err.message : String(err)}`);
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

  async function handleRenameThread() {
    if (!project || !activeThreadId) return;
    const current = chatThreads.find((thread) => thread.thread_id === activeThreadId);
    const title = window.prompt('Chat title', current?.title ?? 'New chat')?.trim();
    if (!title || title === current?.title) return;

    const updated = await api.put<ChatThread>(
      `/api/projects/${project.project_id}/chat_threads/${activeThreadId}`,
      { title }
    );
    setChatThreads((threads) =>
      threads.map((thread) =>
        thread.thread_id === activeThreadId
          ? { ...thread, title: updated.title, updated_at: updated.updated_at }
          : thread
      )
    );
  }

  async function handleDeleteThread() {
    if (!project || !activeThreadId) return;
    const current = chatThreads.find((thread) => thread.thread_id === activeThreadId);
    const confirmed = window.confirm(`Delete chat "${current?.title ?? 'this chat'}"?`);
    if (!confirmed) return;

    await api.delete(`/api/projects/${project.project_id}/chat_threads/${activeThreadId}`);
    await loadChatThreads(project.project_id);
    chat.reset();
    viewport.reset();
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
        <p>Connecting to CAD Agent...</p>
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
          <span className="app-logo">◆</span>
          <h1>CAD Agent</h1>
        </div>
        <div className="app-header-project">
          <div className="project-dropdown">
            <button className="project-dropdown-btn" type="button">
              <div className="project-dropdown-name">{project.name}</div>
              <div className="project-dropdown-time">Created: {formatLocalDateTime(project.created_at)} &nbsp;|&nbsp; Last saved: {formatLocalDateTime(project.updated_at)}</div>
            </button>
            <div className="project-dropdown-menu">
              {projects.map((p) => (
                <button 
                  key={p.project_id} 
                  className={p.project_id === project.project_id ? 'active' : ''}
                  onClick={() => handleProjectChange(p.project_id)}
                  type="button"
                >
                  <div className="project-dropdown-name">{p.name}</div>
                  <div className="project-dropdown-time">Created: {formatLocalDateTime(p.created_at)} &nbsp;|&nbsp; Last saved: {formatLocalDateTime(p.updated_at)}</div>
                </button>
              ))}
            </div>
          </div>
          <button className="header-btn" type="button" onClick={handleNewProject}>
            New project
          </button>
          <button
            className="header-btn constraint-btn-header"
            type="button"
            onClick={() => setConstraintPanelOpen(true)}
            title="Edit project settings and constraints"
          >
            ⚙️ Project Settings
          </button>
        </div>
        <div className="app-header-actions">
          <button
            className="project-path-link"
            type="button"
            onClick={handleOpenProjectFolder}
            title={project.project_path}
          >
          </button>
          
          <div className="export-dropdown">
            <button className="header-btn" type="button">
              📤 Export
            </button>
            <div className="export-menu">
              <button 
                onClick={() => handleExport('step')} 
                disabled={!viewport.currentModelId}
              >
                Download STEP (.step)
              </button>
              <button 
                onClick={() => handleExport('stl')} 
                disabled={!viewport.currentModelId}
              >
                Download STL (.stl)
              </button>
              <button 
                onClick={() => handleExport('glb')} 
                disabled={!viewport.currentModelId}
              >
                Download GLB (.glb)
              </button>
              <button 
                onClick={() => handleExport('source')} 
                disabled={!viewport.currentModelId}
              >
                Download Source (.py)
              </button>
              <hr />
              <button onClick={handleOpenProjectFolder}>
                Open Project Folder
              </button>
            </div>
          </div>

          <span className="connection-dot connected" />
          <span>Connected</span>
        </div>
      </header>

      {/* Main layout */}
      <div className="app-main">
        {historySidebarOpen && (
          <HistorySidebar
            versions={modelVersions}
            onSelect={handleModelVersionChange}
          />
        )}

        {/* 3D Viewport */}
        <div className="app-viewport">
          <button 
            className="viewport-history-toggle"
            onClick={() => setHistorySidebarOpen(!historySidebarOpen)}
            title={historySidebarOpen ? "Close history" : "Open history"}
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
                      {model.model_id} - {formatLocalDateTime(model.created_at)} - {model.prompt || 'checkpoint'}
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
          <AssemblyPanel />
          <FeaturePanel />
          <ParameterPanel />
        </div>

        {/* Chat Panel */}
        <div className="app-chat">
          <div className="chat-thread-bar">
            <select
              className="thread-select"
              value={activeThreadId ?? ''}
              onChange={(e) => handleThreadChange(e.target.value)}
              aria-label="Select chat history"
            >
              {chatThreads.map((thread) => (
                <option key={thread.thread_id} value={thread.thread_id}>
                  {thread.title}
                </option>
              ))}
            </select>
            <div className="chat-thread-actions">
              <button className="chat-thread-new" type="button" onClick={handleNewThread}>
                New chat
              </button>
              <button className="chat-thread-new" type="button" onClick={handleRenameThread}>
                Rename
              </button>
              <button className="chat-thread-new danger" type="button" onClick={handleDeleteThread}>
                Delete
              </button>
            </div>
          </div>
          <Chat onSend={handleSend} disabled={!activeThreadId || !isConnected} />
        </div>
      </div>

      {/* Debug Panel — bottom overlay */}
      <SourcePanel />
      <DebugPanel />
      <ProjectSettingsPanel
        isOpen={constraintPanelOpen}
        onClose={() => setConstraintPanelOpen(false)}
        onRenameProject={handleRenameProject}
        onDeleteProject={handleDeleteProject}
      />
    </div>
  );
}

export default App;
