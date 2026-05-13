/**
 * Tests for Zustand stores: chat, viewport, critique, debug.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { useChatStore, useViewportStore, useCritiqueStore, useDebugStore } from '../stores';

// Helper: reset all store state between tests by calling reset/clear methods
beforeEach(() => {
  useChatStore.getState().reset();
  useViewportStore.getState().reset();
  useCritiqueStore.getState().clearCritique();
  useDebugStore.getState().clear();
});

// ─── Chat Store ────────────────────────────────────────────────────────────

describe('useChatStore', () => {
  it('starts empty', () => {
    const s = useChatStore.getState();
    expect(s.messages).toHaveLength(0);
    expect(s.streamingContent).toBe('');
    expect(s.isGenerating).toBe(false);
  });

  it('addMessage appends correctly', () => {
    const s = useChatStore.getState();
    s.addMessage({ role: 'user', content: 'Hello', timestamp: new Date().toISOString() });
    expect(useChatStore.getState().messages).toHaveLength(1);
    expect(useChatStore.getState().messages[0].content).toBe('Hello');
  });

  it('addMessage appends multiple in order', () => {
    const s = useChatStore.getState();
    s.addMessage({ role: 'user', content: 'A', timestamp: '' });
    s.addMessage({ role: 'assistant', content: 'B', timestamp: '' });
    const msgs = useChatStore.getState().messages;
    expect(msgs[0].content).toBe('A');
    expect(msgs[1].content).toBe('B');
  });

  it('setMessages replaces all', () => {
    const s = useChatStore.getState();
    s.addMessage({ role: 'user', content: 'Old', timestamp: '' });
    s.setMessages([{ role: 'assistant', content: 'New', timestamp: '' }]);
    expect(useChatStore.getState().messages).toHaveLength(1);
    expect(useChatStore.getState().messages[0].content).toBe('New');
  });

  it('appendStreamChunk accumulates', () => {
    const s = useChatStore.getState();
    s.appendStreamChunk('Hello ');
    s.appendStreamChunk('World');
    expect(useChatStore.getState().streamingContent).toBe('Hello World');
  });

  it('clearStream resets streaming content', () => {
    const s = useChatStore.getState();
    s.appendStreamChunk('partial');
    s.clearStream();
    expect(useChatStore.getState().streamingContent).toBe('');
  });

  it('setGenerating(true) sets flag', () => {
    useChatStore.getState().setGenerating(true);
    expect(useChatStore.getState().isGenerating).toBe(true);
  });

  it('setGenerating(false) clears stage/status', () => {
    const s = useChatStore.getState();
    s.setStage('generating', 'Generating code...');
    s.setGenerating(false);
    expect(useChatStore.getState().isGenerating).toBe(false);
    expect(useChatStore.getState().currentStage).toBe('');
    expect(useChatStore.getState().currentStatus).toBe('');
  });

  it('setStage updates stage and status', () => {
    useChatStore.getState().setStage('critiquing', 'Analyzing geometry...');
    expect(useChatStore.getState().currentStage).toBe('critiquing');
    expect(useChatStore.getState().currentStatus).toBe('Analyzing geometry...');
  });

  it('reset returns to initial state', () => {
    const s = useChatStore.getState();
    s.addMessage({ role: 'user', content: 'Hi', timestamp: '' });
    s.appendStreamChunk('foo');
    s.setGenerating(true);
    s.reset();
    const after = useChatStore.getState();
    expect(after.messages).toHaveLength(0);
    expect(after.streamingContent).toBe('');
    expect(after.isGenerating).toBe(false);
  });
});

// ─── Viewport Store ────────────────────────────────────────────────────────

describe('useViewportStore', () => {
  it('starts with null model', () => {
    const s = useViewportStore.getState();
    expect(s.glbUrl).toBeNull();
    expect(s.currentModelId).toBeNull();
    expect(s.isLoading).toBe(false);
  });

  it('setModel updates all fields', () => {
    useViewportStore.getState().setModel('model-001', 'http://localhost/model.glb', 'proj-1');
    const s = useViewportStore.getState();
    expect(s.currentModelId).toBe('model-001');
    expect(s.glbUrl).toBe('http://localhost/model.glb');
    expect(s.currentProjectId).toBe('proj-1');
    expect(s.isLoading).toBe(false);
  });

  it('setLoading(true) sets flag', () => {
    useViewportStore.getState().setLoading(true);
    expect(useViewportStore.getState().isLoading).toBe(true);
  });

  it('reset clears all fields', () => {
    useViewportStore.getState().setModel('model-001', 'http://localhost/x.glb', 'proj');
    useViewportStore.getState().reset();
    const s = useViewportStore.getState();
    expect(s.glbUrl).toBeNull();
    expect(s.currentModelId).toBeNull();
    expect(s.currentProjectId).toBeNull();
  });
});

// ─── Critique Store ────────────────────────────────────────────────────────

describe('useCritiqueStore', () => {
  it('starts with null critique', () => {
    expect(useCritiqueStore.getState().critique).toBeNull();
  });

  it('setCritique stores the critique', () => {
    useCritiqueStore.getState().setCritique({
      score: 0.82,
      matchesIntent: true,
      issues: [],
      renderUrls: { iso: '/api/renders/iso' },
    });
    const c = useCritiqueStore.getState().critique;
    expect(c).not.toBeNull();
    expect(c!.score).toBe(0.82);
    expect(c!.renderUrls.iso).toBe('/api/renders/iso');
  });

  it('setCritique with issues stores them', () => {
    useCritiqueStore.getState().setCritique({
      score: 0.4,
      matchesIntent: false,
      issues: [
        { issue_type: 'thin_wall', severity: 'error', description: 'Wall too thin', location_hint: 'bottom' }
      ],
      renderUrls: {},
    });
    const c = useCritiqueStore.getState().critique;
    expect(c!.issues).toHaveLength(1);
    expect(c!.issues[0].severity).toBe('error');
  });

  it('clearCritique sets null', () => {
    useCritiqueStore.getState().setCritique({ score: 0.5, matchesIntent: true, issues: [], renderUrls: {} });
    useCritiqueStore.getState().clearCritique();
    expect(useCritiqueStore.getState().critique).toBeNull();
  });
});

// ─── Debug Store ───────────────────────────────────────────────────────────

describe('useDebugStore', () => {
  it('starts with empty entries and closed', () => {
    const s = useDebugStore.getState();
    expect(s.entries).toHaveLength(0);
    expect(s.isOpen).toBe(false);
  });

  it('addEntry appends with auto-id', () => {
    useDebugStore.getState().addEntry({
      timestamp: '2026-01-01T00:00:00Z',
      category: 'llm',
      message: 'Request sent',
    });
    const entries = useDebugStore.getState().entries;
    expect(entries).toHaveLength(1);
    expect(entries[0].id).toBeGreaterThan(0);
    expect(entries[0].category).toBe('llm');
  });

  it('addEntry IDs are unique and incrementing', () => {
    const s = useDebugStore.getState();
    s.addEntry({ timestamp: '', category: 'a', message: '1' });
    s.addEntry({ timestamp: '', category: 'b', message: '2' });
    const entries = useDebugStore.getState().entries;
    expect(entries[1].id).toBeGreaterThan(entries[0].id);
  });

  it('toggleOpen flips isOpen', () => {
    useDebugStore.getState().toggleOpen();
    expect(useDebugStore.getState().isOpen).toBe(true);
    useDebugStore.getState().toggleOpen();
    expect(useDebugStore.getState().isOpen).toBe(false);
  });

  it('setOpen sets explicitly', () => {
    useDebugStore.getState().setOpen(true);
    expect(useDebugStore.getState().isOpen).toBe(true);
    useDebugStore.getState().setOpen(false);
    expect(useDebugStore.getState().isOpen).toBe(false);
  });

  it('clear empties entries', () => {
    useDebugStore.getState().addEntry({ timestamp: '', category: 'x', message: 'y' });
    useDebugStore.getState().clear();
    expect(useDebugStore.getState().entries).toHaveLength(0);
  });

  it('addEntry with data stores data field', () => {
    useDebugStore.getState().addEntry({
      timestamp: '',
      category: 'vision',
      message: 'Critique received',
      data: { score: 0.78, issues: 2 },
    });
    const entry = useDebugStore.getState().entries[0];
    expect(entry.data).toEqual({ score: 0.78, issues: 2 });
  });
});
