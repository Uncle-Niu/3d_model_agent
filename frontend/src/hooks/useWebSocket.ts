/**
 * WebSocket hook for real-time communication with the backend.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { useChatStore, useCritiqueStore, useDebugStore, useViewportStore } from '../stores';
import type { WSMessage } from '../types';

export function useWebSocket(projectId: string | null, threadId: string | null) {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const chat = useChatStore();
  const viewport = useViewportStore();
  const debug = useDebugStore();
  const critique = useCritiqueStore();
  const [isConnected, setIsConnected] = useState(false);

  useEffect(() => {
    if (!projectId || !threadId) return;

    const activeProjectId = projectId;
    const activeThreadId = threadId;
    let disposed = false;
    let retry = 0;

    function clearReconnectTimer() {
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    }

    function connect() {
      if (disposed) return;

      const ws = new WebSocket(api.ws(activeProjectId, activeThreadId));
      wsRef.current = ws;

      ws.onopen = () => {
        if (wsRef.current !== ws) return;
        retry = 0;
        setIsConnected(true);
        console.log('[WS] Connected to project:', activeProjectId);
        debug.addEntry({
          timestamp: new Date().toISOString(),
          category: 'ws',
          message: `WebSocket connected to project ${activeProjectId}`,
          data: { thread_id: activeThreadId },
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
        if (wsRef.current !== ws) return;
        console.log('[WS] Disconnected');
        debug.addEntry({
          timestamp: new Date().toISOString(),
          category: 'ws',
          message: 'WebSocket disconnected',
        });
        setIsConnected(false);
        wsRef.current = null;

        if (!disposed) {
          const delay = Math.min(5000, 500 * 2 ** retry);
          retry += 1;
          reconnectTimerRef.current = window.setTimeout(connect, delay);
        }
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
    }

    connect();

    return () => {
      disposed = true;
      clearReconnectTimer();
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      setIsConnected(false);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, threadId]);

  const handleMessage = useCallback((msg: WSMessage) => {
    switch (msg.type) {
      case 'run_state':
        if (msg.running) {
          chat.removeTrailingGeneratingPlaceholder();
          chat.clearStream();
          chat.setGenerating(true);
          chat.setLiveSteps(msg.steps ?? []);
        } else if (chat.isGenerating) {
          chat.setGenerating(false);
        }
        break;

      case 'status':
        console.log(`[WS] Status: ${msg.stage}: ${msg.message}`);
        chat.setStage(msg.stage, msg.message, msg.details, msg.data);
        if (msg.data?.model_id) {
          viewport.setModel(
            msg.data.model_id as string,
            null,
            projectId || '',
            { isWip: true },
          );
        }
        break;

      case 'llm_chunk':
        chat.appendStreamChunk(msg.content);
        break;

      case 'reasoning_chunk':
        chat.appendReasoningChunk(msg.content);
        break;

      case 'model_ready':
        console.log(`[WS] Model ready: ${msg.model_id} -> ${msg.glb_url}`);
        viewport.setModel(
          msg.model_id,
          api.url(msg.glb_url),
          projectId || '',
          { isWip: chat.isGenerating },
        );
        window.dispatchEvent(new CustomEvent('cad-model-ready', {
          detail: { projectId, modelId: msg.model_id },
        }));
        break;

      case 'chat_response':
        chat.clearStream();
        chat.removeTrailingGeneratingPlaceholder();
        chat.addMessage({
          role: 'assistant',
          content: msg.content,
          timestamp: new Date().toISOString(),
          model_id: msg.model_id,
          steps: msg.steps ?? chat.currentSteps,
        });
        chat.setGenerating(false);
        viewport.setWip(false);
        break;

      case 'error':
        console.error(`[WS] Error: ${msg.message}`);
        chat.clearStream();
        chat.addMessage({
          role: 'assistant',
          content: `Error: ${msg.message}`,
          timestamp: new Date().toISOString(),
        });
        chat.setGenerating(false);
        viewport.setWip(false);
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

      case 'critique_result':
        console.log(`[WS] Vision critique: score=${msg.score.toFixed(2)}, issues=${msg.issues.length}`);
        critique.setCritique({
          score: msg.score,
          matchesIntent: msg.matches_intent,
          issues: msg.issues,
          renderUrls: msg.render_urls,
        });
        debug.addEntry({
          timestamp: new Date().toISOString(),
          category: 'vision',
          message: `Vision critique: score=${msg.score.toFixed(2)}, ${msg.issues.length} issue(s)`,
          data: { score: msg.score, issues: msg.issues },
        });
        break;
    }
  }, [projectId, chat, viewport, debug, critique]);

  const sendMessage = useCallback(
    (content: string) => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        console.error('[WS] Not connected');
        chat.addMessage({
          role: 'assistant',
          content: 'Error: chat connection is still reconnecting. Please try again in a moment.',
          timestamp: new Date().toISOString(),
        });
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
        JSON.stringify({
          type: 'chat_message',
          content,
          thread_id: threadId,
          base_model_id: viewport.currentModelId,
        }),
      );
    },
    [chat, threadId, viewport.currentModelId],
  );

  const sendRawMessage = useCallback((msg: object) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    wsRef.current.send(JSON.stringify(msg));
  }, []);

  const cancelChat = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'cancel_chat' }));
      return;
    }

    if (projectId && threadId) {
      void api.post(`/api/projects/${projectId}/chat_threads/${threadId}/cancel`).catch((err) => {
        console.error('[WS] Failed to cancel via REST:', err);
      });
    }
  }, [projectId, threadId]);

  return { sendMessage, sendRawMessage, cancelChat, isConnected };
}
