/**
 * WebSocket hook for real-time communication with the backend.
 */

import { useCallback, useEffect, useRef } from 'react';
import { api } from '../api';
import { useChatStore, useDebugStore, useViewportStore } from '../stores';
import type { WSMessage } from '../types';

export function useWebSocket(projectId: string | null) {
  const wsRef = useRef<WebSocket | null>(null);
  const chat = useChatStore();
  const viewport = useViewportStore();
  const debug = useDebugStore();

  // Connect to WebSocket
  useEffect(() => {
    if (!projectId) return;

    const ws = new WebSocket(api.ws(projectId));
    wsRef.current = ws;

    ws.onopen = () => {
      console.log('[WS] Connected to project:', projectId);
      debug.addEntry({
        timestamp: new Date().toISOString(),
        category: 'ws',
        message: `WebSocket connected to project ${projectId}`,
      });
    };

    ws.onmessage = (event) => {
      try {
        const msg: WSMessage = JSON.parse(event.data);
        handleMessage(msg);
      } catch (e) {
        console.error('[WS] Failed to parse message:', e);
      }
    };

    ws.onclose = () => {
      console.log('[WS] Disconnected');
      debug.addEntry({
        timestamp: new Date().toISOString(),
        category: 'ws',
        message: 'WebSocket disconnected',
      });
      wsRef.current = null;
    };

    ws.onerror = (err) => {
      console.error('[WS] Error:', err);
      debug.addEntry({
        timestamp: new Date().toISOString(),
        category: 'ws',
        message: 'WebSocket error',
        data: { error: String(err) },
      });
    };

    return () => {
      ws.close();
      wsRef.current = null;
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const handleMessage = useCallback((msg: WSMessage) => {
    switch (msg.type) {
      case 'status':
        console.log(`[WS] Status: ${msg.stage} — ${msg.message}`);
        chat.setStage(msg.stage, msg.message);
        break;

      case 'llm_chunk':
        chat.appendStreamChunk(msg.content);
        break;

      case 'model_ready':
        console.log(`[WS] Model ready: ${msg.model_id} → ${msg.glb_url}`);
        viewport.setModel(msg.model_id, api.url(msg.glb_url), projectId || '');
        break;

      case 'chat_response':
        // Finalize: move streaming content to a proper message
        chat.clearStream();
        chat.addMessage({
          role: 'assistant',
          content: msg.content,
          timestamp: new Date().toISOString(),
        });
        chat.setGenerating(false);
        break;

      case 'error':
        console.error(`[WS] Error: ${msg.message}`);
        chat.clearStream();
        chat.addMessage({
          role: 'assistant',
          content: `❌ Error: ${msg.message}`,
          timestamp: new Date().toISOString(),
        });
        chat.setGenerating(false);
        break;

      case 'debug_log':
        console.log(`[DEBUG][${msg.category}] ${msg.message}`, msg.data || '');
        debug.addEntry({
          timestamp: msg.timestamp,
          category: msg.category,
          message: msg.message,
          data: msg.data,
        });
        break;
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Send a chat message
  const sendMessage = useCallback(
    (content: string) => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        console.error('[WS] Not connected');
        return;
      }

      chat.addMessage({
        role: 'user',
        content,
        timestamp: new Date().toISOString(),
      });
      chat.setGenerating(true);
      chat.clearStream();

      wsRef.current.send(
        JSON.stringify({ type: 'chat_message', content })
      );
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    []
  );

  return { sendMessage, isConnected: !!wsRef.current };
}
