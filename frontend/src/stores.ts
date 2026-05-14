/**
 * Zustand stores for application state.
 */

import { create } from 'zustand';
import type { ChatMessage, CritiqueState, DebugEntry, GeometryIssue, Project } from './types';
import type { Vector3 } from 'three';


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
  currentSteps: PipelineStep[];
  setMessages: (messages: ChatMessage[]) => void;
  addMessage: (msg: ChatMessage) => void;
  appendStreamChunk: (chunk: string) => void;
  clearStream: () => void;
  setGenerating: (generating: boolean) => void;
  setStage: (stage: string, status: string, details?: string, data?: any) => void;
  reset: () => void;
}

export const useChatStore = create<ChatState>((set) => ({
  messages: [],
  streamingContent: '',
  isGenerating: false,
  currentStage: '',
  currentStatus: '',
  currentSteps: [],

  setMessages: (messages) => set({ messages }),

  addMessage: (msg) =>
    set((s) => ({ messages: [...s.messages, msg] })),

  appendStreamChunk: (chunk) =>
    set((s) => ({ streamingContent: s.streamingContent + chunk })),

  clearStream: () => set({ streamingContent: '' }),

  setGenerating: (generating) =>
    set({ isGenerating: generating, ...(generating ? { currentSteps: [] } : { currentStage: '', currentStatus: '', currentSteps: [] }) }),

  setStage: (stage, status, details, data) =>
    set((s) => ({ 
      currentStage: stage, 
      currentStatus: status,
      currentSteps: [
        ...s.currentSteps, 
        { stage, message: status, details, data, timestamp: new Date().toISOString() }
      ]
    })),

  reset: () =>
    set({
      messages: [],
      streamingContent: '',
      isGenerating: false,
      currentStage: '',
      currentStatus: '',
      currentSteps: [],
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
  setProjectId: (projectId: string | null) => void;
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

  setProjectId: (projectId) => set({ currentProjectId: projectId }),

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

// ---------------------------------------------------------------------------
// Critique store — vision model feedback
// ---------------------------------------------------------------------------

interface CritiqueStoreState {
  critique: CritiqueState | null;
  setCritique: (c: CritiqueState | null) => void;
  clearCritique: () => void;
}

export const useCritiqueStore = create<CritiqueStoreState>((set) => ({
  critique: null,
  setCritique: (critique) => set({ critique }),
  clearCritique: () => set({ critique: null }),
}));

// ---------------------------------------------------------------------------
// Selection store — viewport geometry selection
// ---------------------------------------------------------------------------

interface SelectionState {
  selectedFeatureName: string | null;
  selectedPoint: [number, number, number] | null;
  setSelection: (featureName: string | null, point?: Vector3 | null) => void;
  clearSelection: () => void;
}

export const useSelectionStore = create<SelectionState>((set) => ({
  selectedFeatureName: null,
  selectedPoint: null,

  setSelection: (featureName, point) =>
    set({
      selectedFeatureName: featureName,
      selectedPoint: point ? [point.x, point.y, point.z] : null,
    }),

  clearSelection: () =>
    set({ selectedFeatureName: null, selectedPoint: null }),
}));

// ---------------------------------------------------------------------------
// Assembly store — part visibility and exploded views
// ---------------------------------------------------------------------------

interface PartVisibility {
  [name: string]: boolean;
}

interface AssemblyState {
  partsVisibility: PartVisibility;
  explodedFactor: number;
  setParts: (partNames: string[]) => void;
  toggleVisibility: (name: string) => void;
  setVisibility: (name: string, visible: boolean) => void;
  setExplodedFactor: (factor: number) => void;
  reset: () => void;
}

export const useAssemblyStore = create<AssemblyState>((set) => ({
  partsVisibility: {},
  explodedFactor: 0,

  setParts: (partNames) => {
    const visibility: PartVisibility = {};
    partNames.forEach((name) => {
      visibility[name] = true;
    });
    set({ partsVisibility: visibility });
  },

  toggleVisibility: (name) =>
    set((s) => ({
      partsVisibility: {
        ...s.partsVisibility,
        [name]: !s.partsVisibility[name],
      },
    })),

  setVisibility: (name, visible) =>
    set((s) => ({
      partsVisibility: {
        ...s.partsVisibility,
        [name]: visible,
      },
    })),

  setExplodedFactor: (factor) => set({ explodedFactor: factor }),

  reset: () => set({ partsVisibility: {}, explodedFactor: 0 }),
}));

