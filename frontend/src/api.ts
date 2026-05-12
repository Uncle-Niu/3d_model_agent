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
  ws(projectId: string, threadId?: string | null): string {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const query = threadId ? `?thread_id=${encodeURIComponent(threadId)}` : '';
    return `${proto}://${window.location.host}/ws/${projectId}${query}`;
  },

  /** Fetch wrapper with JSON parsing */
  async get<T = unknown>(path: string): Promise<T> {
    const res = await fetch(path);
    if (!res.ok) throw new Error(`GET ${path} failed: ${res.status}`);
    return res.json();
  },

  async getText(path: string): Promise<string> {
    const res = await fetch(path);
    if (!res.ok) throw new Error(`GET ${path} failed: ${res.status}`);
    return res.text();
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

  async delete<T = unknown>(path: string): Promise<T> {
    const res = await fetch(path, { method: 'DELETE' });
    if (!res.ok) throw new Error(`DELETE ${path} failed: ${res.status}`);
    return res.json();
  },

  /** Download a file by triggering browser download */
  async downloadFile(path: string, filename: string): Promise<void> {
    try {
      console.log(`[Download] Starting download from: ${path}`);
      const res = await fetch(path);
      console.log(`[Download] Response status: ${res.status}, ok: ${res.ok}`);
      
      if (!res.ok) {
        const errorText = await res.text();
        console.error(`[Download] Server error response:`, errorText);
        throw new Error(`Download failed with status ${res.status}: ${errorText}`);
      }
      
      const blob = await res.blob();
      console.log(`[Download] Blob received, size: ${blob.size} bytes, type: ${blob.type}`);
      
      if (blob.size === 0) {
        throw new Error('Downloaded file is empty');
      }
      
      const url = URL.createObjectURL(blob);
      console.log(`[Download] Object URL created: ${url}`);
      
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      console.log(`[Download] Triggering download with filename: ${filename}`);
      
      a.click();
      
      // Clean up after a short delay to allow download to start
      setTimeout(() => {
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        console.log(`[Download] Cleanup completed`);
      }, 100);
      
      console.log(`[Download] Download triggered successfully`);
    } catch (err) {
      console.error('[Download] Error during download:', err);
      throw err;
    }
  },
};
