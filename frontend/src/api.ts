/**
 * API configuration and helper functions.
 *
 * Uses relative URLs — Vite dev proxy forwards to the backend.
 */

export const api = {
  /** Build API URL (relative, proxied by Vite) */
  url(path: string): string {
    return path;
  },

  /** WebSocket URL for a project */
  ws(projectId: string): string {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    return `${proto}://${window.location.host}/ws/${projectId}`;
  },

  /** Fetch wrapper with JSON parsing */
  async get<T = unknown>(path: string): Promise<T> {
    const res = await fetch(path);
    if (!res.ok) throw new Error(`GET ${path} failed: ${res.status}`);
    return res.json();
  },

  async post<T = unknown>(path: string, body?: unknown): Promise<T> {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) throw new Error(`POST ${path} failed: ${res.status}`);
    return res.json();
  },

  async put<T = unknown>(path: string, body?: unknown): Promise<T> {
    const res = await fetch(path, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) throw new Error(`PUT ${path} failed: ${res.status}`);
    return res.json();
  },

  /** Download a file by triggering browser download */
  async downloadFile(path: string, filename: string): Promise<void> {
    try {
      const res = await fetch(path);
      if (!res.ok) throw new Error(`Download failed: ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error('Download error:', err);
      throw err;
    }
  },
};
