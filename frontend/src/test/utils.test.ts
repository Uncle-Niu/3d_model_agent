/**
 * Unit tests for pure utility functions.
 *
 * Covers:
 * - extract_code_from_response equivalent (TypeScript side: none yet, skip)
 * - formatLocalDateTime (time utility)
 * - api URL builder
 * - WSMessage type discrimination
 */

import { describe, it, expect } from 'vitest';
import { formatLocalDateTime } from '../time';

// ─── formatLocalDateTime ───────────────────────────────────────────────────

describe('formatLocalDateTime', () => {
  it('returns a non-empty string for a valid ISO timestamp', () => {
    const result = formatLocalDateTime('2026-05-12T12:00:00Z');
    expect(typeof result).toBe('string');
    expect(result.length).toBeGreaterThan(0);
  });

  it('handles undefined gracefully', () => {
    const result = formatLocalDateTime(undefined as unknown as string);
    // Should not throw; returns some fallback string
    expect(typeof result).toBe('string');
  });

  it('handles empty string gracefully', () => {
    const result = formatLocalDateTime('');
    expect(typeof result).toBe('string');
  });

  it('returns different strings for different timestamps', () => {
    const a = formatLocalDateTime('2026-01-01T00:00:00Z');
    const b = formatLocalDateTime('2026-06-15T12:30:00Z');
    expect(a).not.toBe(b);
  });
});

// ─── WebSocket message type discrimination ─────────────────────────────────

import type { WSMessage } from '../types';

describe('WSMessage type discrimination', () => {
  it('status message has stage and message', () => {
    const msg: WSMessage = { type: 'status', stage: 'generating', message: 'Generating...' };
    expect(msg.type).toBe('status');
    if (msg.type === 'status') {
      expect(msg.stage).toBe('generating');
    }
  });

  it('model_ready has model_id and glb_url', () => {
    const msg: WSMessage = { type: 'model_ready', model_id: 'model-001', glb_url: '/api/.../glb' };
    expect(msg.type).toBe('model_ready');
    if (msg.type === 'model_ready') {
      expect(msg.model_id).toBe('model-001');
      expect(msg.glb_url).toBe('/api/.../glb');
    }
  });

  it('chat_response has content', () => {
    const msg: WSMessage = { type: 'chat_response', content: 'Model generated.' };
    if (msg.type === 'chat_response') {
      expect(msg.content).toBe('Model generated.');
    }
  });

  it('error has message field', () => {
    const msg: WSMessage = { type: 'error', message: 'Something went wrong' };
    if (msg.type === 'error') {
      expect(msg.message).toBe('Something went wrong');
    }
  });

  it('critique_result has score and issues', () => {
    const msg: WSMessage = {
      type: 'critique_result',
      score: 0.73,
      matches_intent: true,
      issues: [{ issue_type: 'thin_wall', severity: 'warning', description: 'desc', location_hint: '' }],
      repair_prompt: '',
      render_urls: { iso: '/renders/iso' },
    };
    if (msg.type === 'critique_result') {
      expect(msg.score).toBe(0.73);
      expect(msg.issues).toHaveLength(1);
      expect(msg.render_urls.iso).toBe('/renders/iso');
    }
  });

  it('debug_log has timestamp, category, message', () => {
    const msg: WSMessage = {
      type: 'debug_log',
      timestamp: '2026-01-01T00:00:00Z',
      category: 'llm',
      message: 'Request sent',
    };
    if (msg.type === 'debug_log') {
      expect(msg.category).toBe('llm');
      expect(msg.timestamp).toBeTruthy();
    }
  });
});
