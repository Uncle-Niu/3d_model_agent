/**
 * Main App component — layout with viewport and chat panel.
 */

import { useEffect, useState } from 'react';
import Chat from './components/Chat';
import Viewport from './components/Viewport';
import { useWebSocket } from './hooks/useWebSocket';
import { useProjectStore } from './stores';
import { api } from './api';
import type { Project } from './types';

function App() {
  const { project, setProject } = useProjectStore();
  const [initializing, setInitializing] = useState(true);
  const { sendMessage } = useWebSocket(project?.project_id ?? null);

  // On mount: create or load a project
  useEffect(() => {
    initProject();
  }, []);

  async function initProject() {
    try {
      // Try to load existing projects
      const projects = await api.get<Project[]>('/api/projects');
      if (projects.length > 0) {
        setProject(projects[0]);
      } else {
        // Create a new project
        const newProject = await api.post<Project>('/api/projects', {
          name: 'My First Project',
        });
        setProject(newProject);
      }
    } catch (err) {
      console.error('Failed to initialize project:', err);
    } finally {
      setInitializing(false);
    }
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
          {project.name}
        </div>
        <div className="app-header-actions">
          <span className="connection-dot connected" />
          <span>Connected</span>
        </div>
      </header>

      {/* Main layout */}
      <div className="app-main">
        {/* 3D Viewport */}
        <div className="app-viewport">
          <Viewport />
        </div>

        {/* Chat Panel */}
        <div className="app-chat">
          <Chat onSend={sendMessage} />
        </div>
      </div>
    </div>
  );
}

export default App;
