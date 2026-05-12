/**
 * Zustand stores for application state.
 */

import { create } from 'zustand';
import type { ChatMessage, DebugEntry, Project } from './types';

// ---------------------------------------------------------------------------
// Project store
// ---------------------------------------------------------------------------

interface ProjectState {
  project: Project | null;
  setProject: (project: Project | null) => void;
}

export const useProjectStore = create<ProjectState>((set) => ({
  project: null,
  setProject: (project) => set({ project }),
}));

// ---------------------------------------------------------------------------
// Chat store
// ---------------------------------------------------------------------------

interface ChatState {
  messages: ChatMessage[];
  streamingContent: string;
  isGenerating: boolean;
  currentStage: string;
  currentStatus: string;
  addMessage: (msg: ChatMessage) => void;
  appendStreamChunk: (chunk: string) => void;
  clearStream: () => void;
  setGenerating: (generating: boolean) => void;
  setStage: (stage: string, status: string) => void;
  reset: () => void;
}

export const useChatStore = create<ChatState>((set) => ({
  messages: [],
  streamingContent: '',
  isGenerating: false,
  currentStage: '',
  currentStatus: '',

  addMessage: (msg) =>
    set((s) => ({ messages: [...s.messages, msg] })),

  appendStreamChunk: (chunk) =>
    set((s) => ({ streamingContent: s.streamingContent + chunk })),

  clearStream: () => set({ streamingContent: '' }),

  setGenerating: (generating) =>
    set({ isGenerating: generating, ...(generating ? {} : { currentStage: '', currentStatus: '' }) }),

  setStage: (stage, status) =>
    set({ currentStage: stage, currentStatus: status }),

  reset: () =>
    set({
      messages: [],
      streamingContent: '',
      isGenerating: false,
      currentStage: '',
      currentStatus: '',
    }),
}));

// ---------------------------------------------------------------------------
// Viewport store
// ---------------------------------------------------------------------------

interface ViewportState {
  glbUrl: string | null;
  currentModelId: string | null;
  currentProjectId: string | null;
  isLoading: boolean;
  setModel: (modelId: string, glbUrl: string, projectId: string) => void;
  setLoading: (loading: boolean) => void;
  reset: () => void;
}

export const useViewportStore = create<ViewportState>((set) => ({
  glbUrl: null,
  currentModelId: null,
  currentProjectId: null,
  isLoading: false,

  setModel: (modelId, glbUrl, projectId) =>
    set({ currentModelId: modelId, glbUrl, currentProjectId: projectId, isLoading: false }),

  setLoading: (loading) => set({ isLoading: loading }),

  reset: () =>
    set({ glbUrl: null, currentModelId: null, currentProjectId: null, isLoading: false }),
}));

// ---------------------------------------------------------------------------
// Debug store — raw LLM request/response log
// ---------------------------------------------------------------------------

let _debugId = 0;

interface DebugState {
  entries: DebugEntry[];
  isOpen: boolean;
  addEntry: (entry: Omit<DebugEntry, 'id'>) => void;
  toggleOpen: () => void;
  setOpen: (open: boolean) => void;
  clear: () => void;
}

export const useDebugStore = create<DebugState>((set) => ({
  entries: [],
  isOpen: false,

  addEntry: (entry) =>
    set((s) => ({
      entries: [...s.entries, { ...entry, id: ++_debugId }],
    })),

  toggleOpen: () => set((s) => ({ isOpen: !s.isOpen })),
  setOpen: (open) => set({ isOpen: open }),

  clear: () => set({ entries: [] }),
}));
