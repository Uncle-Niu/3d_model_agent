/**
 * BottomDock — unified bottom panel hosting Source, Assembly, Features,
 * Parameters, and Debug as switchable tabs. Always shows the tab strip
 * even when collapsed so the surfaces are discoverable.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api';
import { useDebugStore, useViewportStore } from '../stores';
import { formatLocalDateTime } from '../time';
import type { DebugEntry, ModelInfo } from '../types';
import AssemblyPanel from './AssemblyPanel';
import FeaturePanel from './FeaturePanel';
import ParameterPanel from './ParameterPanel';
import { toast } from './ui/Toast';

type DockTab = 'source' | 'assembly' | 'features' | 'parameters' | 'debug';

type DiffLine = {
  type: 'same' | 'added' | 'removed';
  text: string;
  oldLine?: number;
  newLine?: number;
};

type SideBySideDiffRow = {
  type: 'same' | 'added' | 'removed' | 'changed';
  leftText: string;
  rightText: string;
  leftLine?: number;
  rightLine?: number;
};

type ExecuteSourceResponse = {
  success: boolean;
  message: string;
  model: ModelInfo;
  glb_url?: string | null;
  violations: string[];
};

const CATEGORY_COLORS: Record<string, string> = {
  ollama: '#4caf7a',
  llm_request: '#e0a040',
  llm_response: '#4a90d9',
  llm_error: '#d94a5a',
  code_extraction: '#9b59b6',
  cadquery_exec: '#3498db',
  cadquery_result: '#1abc9c',
  repair_request: '#e67e22',
  repair_response: '#f39c12',
  model_ready: '#2ecc71',
  pipeline_error: '#e74c3c',
  init: '#7f8c8d',
  ws: '#95a5a6',
  error: '#d94a5a',
};

function buildLineDiff(oldText: string, newText: string): DiffLine[] {
  const oldLines = oldText.split('\n');
  const newLines = newText.split('\n');
  const dp: number[][] = Array.from({ length: oldLines.length + 1 }, () =>
    Array(newLines.length + 1).fill(0)
  );
  for (let i = oldLines.length - 1; i >= 0; i -= 1) {
    for (let j = newLines.length - 1; j >= 0; j -= 1) {
      dp[i][j] = oldLines[i] === newLines[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const diff: DiffLine[] = [];
  let i = 0, j = 0, oldLine = 1, newLine = 1;
  while (i < oldLines.length && j < newLines.length) {
    if (oldLines[i] === newLines[j]) {
      diff.push({ type: 'same', text: oldLines[i], oldLine, newLine });
      i++; j++; oldLine++; newLine++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      diff.push({ type: 'removed', text: oldLines[i], oldLine });
      i++; oldLine++;
    } else {
      diff.push({ type: 'added', text: newLines[j], newLine });
      j++; newLine++;
    }
  }
  while (i < oldLines.length) { diff.push({ type: 'removed', text: oldLines[i], oldLine }); i++; oldLine++; }
  while (j < newLines.length) { diff.push({ type: 'added', text: newLines[j], newLine }); j++; newLine++; }
  return diff;
}

function buildSideBySideDiff(leftText: string, rightText: string): SideBySideDiffRow[] {
  const diff = buildLineDiff(leftText, rightText);
  const rows: SideBySideDiffRow[] = [];
  for (let i = 0; i < diff.length; i++) {
    const line = diff[i];
    const next = diff[i + 1];
    if (line.type === 'same') {
      rows.push({ type: 'same', leftText: line.text, rightText: line.text, leftLine: line.oldLine, rightLine: line.newLine });
    } else if (line.type === 'removed' && next?.type === 'added') {
      rows.push({ type: 'changed', leftText: line.text, rightText: next.text, leftLine: line.oldLine, rightLine: next.newLine });
      i++;
    } else if (line.type === 'removed') {
      rows.push({ type: 'removed', leftText: line.text, rightText: '', leftLine: line.oldLine });
    } else {
      rows.push({ type: 'added', leftText: '', rightText: line.text, rightLine: line.newLine });
    }
  }
  return rows;
}

function shortId(id: string, n = 6) {
  return id.length <= n ? id : id.slice(-n);
}

// ---------------------------------------------------------------------------
// Debug entries view
// ---------------------------------------------------------------------------

function DebugEntryRow({ entry, isExpanded, onToggle }: {
  entry: DebugEntry;
  isExpanded: boolean;
  onToggle: () => void;
}) {
  return (
    <div className="debug-entry">
      <div className="debug-entry-header" onClick={onToggle}>
        <span className="debug-ts">{formatLocalDateTime(entry.timestamp)}</span>
        <span className="debug-badge" style={{ background: CATEGORY_COLORS[entry.category] || '#606080' }}>
          {entry.category}
        </span>
        <span className="debug-msg">{entry.message}</span>
        {entry.data && (
          <span className="debug-expand-icon">{isExpanded ? '▾' : '▸'}</span>
        )}
      </div>
      {isExpanded && entry.data && (
        <div className="debug-entry-data">
          <pre>{JSON.stringify(entry.data, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}

function DebugView() {
  const { entries, clear } = useDebugStore();
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [entries]);

  function toggle(id: number) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  return (
    <div className="debug-view">
      <div className="debug-view-actions">
        <span className="debug-count">{entries.length} entries</span>
        <button className="btn btn-ghost btn-sm" onClick={() => setExpanded(new Set(entries.map((e) => e.id)))}>Expand all</button>
        <button className="btn btn-ghost btn-sm" onClick={() => setExpanded(new Set())}>Collapse all</button>
        <button className="btn btn-ghost btn-sm" onClick={clear}>Clear</button>
      </div>
      <div className="debug-entries" ref={scrollRef}>
        {entries.length === 0 ? (
          <div className="dock-empty">No debug messages yet. Send a chat message to see raw LLM request/response data.</div>
        ) : (
          entries.map((entry) => (
            <DebugEntryRow key={entry.id} entry={entry} isExpanded={expanded.has(entry.id)} onToggle={() => toggle(entry.id)} />
          ))
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main dock
// ---------------------------------------------------------------------------

export default function BottomDock() {
  const { currentModelId, currentProjectId } = useViewportStore();
  const viewport = useViewportStore();
  const debugEntries = useDebugStore((s) => s.entries);

  const [isOpen, setIsOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<DockTab>('source');

  // Source state
  const [source, setSource] = useState('');
  const [savedSource, setSavedSource] = useState('');
  const [modelVersions, setModelVersions] = useState<ModelInfo[]>([]);
  const [leftModelId, setLeftModelId] = useState('');
  const [rightModelId, setRightModelId] = useState('');
  const [leftSource, setLeftSource] = useState('');
  const [rightSource, setRightSource] = useState('');
  const [viewMode, setViewMode] = useState<'source' | 'diff'>('source');
  const [error, setError] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [isExecuting, setIsExecuting] = useState(false);

  const [panelHeight, setPanelHeight] = useState(() => {
    const stored = Number(localStorage.getItem('sourcePanelHeight'));
    return Number.isFinite(stored) && stored >= 220 ? stored : 380;
  });

  // Load source for current model when switching to source tab
  useEffect(() => {
    if (!isOpen || activeTab !== 'source' || !currentProjectId) return;

    if (!currentModelId) {
      const defaultSource = 'import cadquery as cq\n\n# Start a new CAD model from scratch\nresult = cq.Workplane("XY").box(10, 10, 10)\n';
      setSource(defaultSource);
      setSavedSource(defaultSource);
      setError('');
      return;
    }

    let cancelled = false;
    setIsLoading(true);
    setError('');

    api.getText(`/api/projects/${currentProjectId}/models/${currentModelId}/source`)
      .then((text) => {
        if (cancelled) return;
        setSource(text);
        setSavedSource(text);
      })
      .catch((err) => {
        if (cancelled) return;
        setSource('');
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });

    return () => { cancelled = true; };
  }, [isOpen, activeTab, currentProjectId, currentModelId]);

  // Load model versions for diff dropdowns
  useEffect(() => {
    if (!isOpen || activeTab !== 'source' || !currentProjectId) return;
    let cancelled = false;
    api.get<ModelInfo[]>(`/api/projects/${currentProjectId}/models`)
      .then((models) => {
        if (cancelled) return;
        const successful = models.filter((m) => m.has_glb);
        setModelVersions(successful);
        const currentIndex = successful.findIndex((m) => m.model_id === currentModelId);
        const previous = currentIndex > 0 ? successful[currentIndex - 1] : successful[0];
        setLeftModelId((existing) => {
          if (existing && successful.some((m) => m.model_id === existing)) return existing;
          if (previous && previous.model_id !== currentModelId) return previous.model_id;
          return successful.find((m) => m.model_id !== currentModelId)?.model_id ?? '';
        });
        setRightModelId((existing) => {
          if (existing && successful.some((m) => m.model_id === existing)) return existing;
          return currentModelId ?? successful.at(-1)?.model_id ?? '';
        });
      })
      .catch((err) => console.error('Failed to load model versions for source diff:', err));
    return () => { cancelled = true; };
  }, [isOpen, activeTab, currentProjectId, currentModelId]);

  useEffect(() => {
    if (!isOpen || activeTab !== 'source' || viewMode !== 'diff' || !currentProjectId || !leftModelId) return;
    let cancelled = false;
    if (leftModelId === currentModelId) { setLeftSource(source); return; }
    api.getText(`/api/projects/${currentProjectId}/models/${leftModelId}/source`)
      .then((text) => { if (!cancelled) setLeftSource(text); })
      .catch((err) => { if (!cancelled) setError(err instanceof Error ? err.message : String(err)); });
    return () => { cancelled = true; };
  }, [isOpen, activeTab, viewMode, currentProjectId, currentModelId, leftModelId, source]);

  useEffect(() => {
    if (!isOpen || activeTab !== 'source' || viewMode !== 'diff' || !currentProjectId || !rightModelId) return;
    let cancelled = false;
    if (rightModelId === currentModelId) { setRightSource(source); return; }
    api.getText(`/api/projects/${currentProjectId}/models/${rightModelId}/source`)
      .then((text) => { if (!cancelled) setRightSource(text); })
      .catch((err) => { if (!cancelled) setError(err instanceof Error ? err.message : String(err)); });
    return () => { cancelled = true; };
  }, [isOpen, activeTab, viewMode, currentProjectId, currentModelId, rightModelId, source]);

  async function copySource() {
    if (!source) return;
    await navigator.clipboard.writeText(source);
    toast.success('Source copied to clipboard');
  }

  function revertSource() {
    setSource(savedSource);
    toast.info('Reverted unsaved changes');
  }

  async function executeSource() {
    if (!currentProjectId || !source.trim()) return;
    setIsExecuting(true);
    setError('');
    try {
      const result = await api.post<ExecuteSourceResponse>(
        `/api/projects/${currentProjectId}/models/execute_source`,
        {
          source,
          prompt: currentModelId ? `Manual edit from ${currentModelId}` : 'Manual source edit',
        }
      );
      if (!result.success) {
        toast.error(`Execution failed: ${result.message}`);
        return;
      }
      setSavedSource(source);
      toast.success(`Executed as #${shortId(result.model.model_id)}`);
      if (result.glb_url) {
        viewport.setModel(result.model.model_id, api.url(result.glb_url), currentProjectId);
      }
      window.dispatchEvent(new CustomEvent('cad-model-ready', {
        detail: { projectId: currentProjectId, modelId: result.model.model_id },
      }));
    } catch (err) {
      toast.error(`Execution failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setIsExecuting(false);
    }
  }

  function handleResizeStart(event: React.PointerEvent<HTMLDivElement>) {
    event.preventDefault();
    const startY = event.clientY;
    const startHeight = panelHeight;
    function handleMove(moveEvent: PointerEvent) {
      const nextHeight = Math.min(720, Math.max(220, startHeight + startY - moveEvent.clientY));
      setPanelHeight(nextHeight);
      localStorage.setItem('sourcePanelHeight', String(nextHeight));
    }
    function handleEnd() {
      window.removeEventListener('pointermove', handleMove);
      window.removeEventListener('pointerup', handleEnd);
    }
    window.addEventListener('pointermove', handleMove);
    window.addEventListener('pointerup', handleEnd);
  }

  const diffRows = useMemo(() => buildSideBySideDiff(leftSource, rightSource), [leftSource, rightSource]);
  const addedCount = diffRows.filter((line) => line.type === 'added' || line.type === 'changed').length;
  const removedCount = diffRows.filter((line) => line.type === 'removed' || line.type === 'changed').length;
  const isDirty = source !== savedSource;

  function openTab(tab: DockTab) {
    setActiveTab(tab);
    setIsOpen(true);
  }

  const tabs: Array<{ id: DockTab; label: string; badge?: number }> = [
    { id: 'source', label: 'Source' },
    { id: 'assembly', label: 'Assembly' },
    { id: 'features', label: 'Features' },
    { id: 'parameters', label: 'Parameters' },
    { id: 'debug', label: 'Debug', badge: debugEntries.length },
  ];

  return (
    <div
      className={`dock ${isOpen ? 'is-open' : ''}`}
      style={isOpen ? { height: panelHeight } : undefined}
    >
      {isOpen && (
        <div
          className="dock-resize"
          onPointerDown={handleResizeStart}
          title="Drag to resize"
        >
          <span className="dock-resize-grip" />
        </div>
      )}

      <div className="dock-tab-bar">
        <div className="dock-tabs">
          {tabs.map((t) => (
            <button
              key={t.id}
              className={`dock-tab ${activeTab === t.id && isOpen ? 'is-active' : ''}`}
              onClick={() => activeTab === t.id && isOpen ? setIsOpen(false) : openTab(t.id)}
            >
              {t.label}
              {t.badge !== undefined && t.badge > 0 && (
                <span className="dock-tab-badge">{t.badge}</span>
              )}
            </button>
          ))}
        </div>

        <div className="dock-toolbar">
          {isOpen && activeTab === 'source' && source && (
            <>
              <button
                className={`btn btn-ghost btn-sm ${viewMode === 'source' ? 'is-active' : ''}`}
                onClick={() => setViewMode('source')}
              >
                Edit
              </button>
              <button
                className={`btn btn-ghost btn-sm ${viewMode === 'diff' ? 'is-active' : ''}`}
                onClick={() => setViewMode('diff')}
              >
                Diff
              </button>
              <button className="btn btn-ghost btn-sm" onClick={copySource}>Copy</button>
              {isDirty && (
                <button className="btn btn-ghost btn-sm" onClick={revertSource} title="Discard unsaved edits">
                  Revert
                </button>
              )}
              <button
                className="btn btn-primary btn-sm"
                onClick={executeSource}
                disabled={isExecuting || !source.trim()}
              >
                {isExecuting ? 'Executing…' : isDirty ? 'Save & Execute' : 'Execute'}
              </button>
            </>
          )}
          <button
            className="btn btn-ghost btn-sm"
            onClick={() => setIsOpen((open) => !open)}
            title={isOpen ? 'Collapse panel' : 'Expand panel'}
          >
            {isOpen ? '▾' : '▴'}
          </button>
        </div>
      </div>

      {isOpen && (
        <div className="dock-content">
          {activeTab === 'assembly' && <AssemblyPanel insideDock />}
          {activeTab === 'features' && <FeaturePanel insideDock />}
          {activeTab === 'parameters' && <ParameterPanel insideDock />}
          {activeTab === 'debug' && <DebugView />}
          {activeTab === 'source' && (
            <>
              {!currentProjectId ? (
                <div className="dock-empty">No project selected.</div>
              ) : isLoading && currentModelId ? (
                <div className="dock-empty">Loading source…</div>
              ) : error ? (
                <div className="dock-error">{error}</div>
              ) : viewMode === 'diff' ? (
                <div className="source-diff-view">
                  <div className="source-diff-controls">
                    <span>Left</span>
                    <select
                      value={leftModelId}
                      onChange={(e) => setLeftModelId(e.target.value)}
                      aria-label="Select left source version"
                    >
                      {modelVersions.map((m) => (
                        <option key={m.model_id} value={m.model_id}>
                          #{shortId(m.model_id)} — {formatLocalDateTime(m.created_at)} — {m.prompt || 'checkpoint'}
                        </option>
                      ))}
                    </select>
                    <span>Right</span>
                    <select
                      value={rightModelId}
                      onChange={(e) => setRightModelId(e.target.value)}
                      aria-label="Select right source version"
                    >
                      {modelVersions.map((m) => (
                        <option key={m.model_id} value={m.model_id}>
                          #{shortId(m.model_id)} — {formatLocalDateTime(m.created_at)} — {m.prompt || 'checkpoint'}
                        </option>
                      ))}
                    </select>
                    <span className="source-diff-stats">+{addedCount} / -{removedCount}</span>
                  </div>
                  {!leftModelId || !rightModelId ? (
                    <div className="dock-empty">Pick left and right model versions to compare.</div>
                  ) : (
                    <div className="source-diff-side-by-side">
                      <div className="source-diff-heading">
                        <span>#{shortId(leftModelId)}</span>
                        <span>#{shortId(rightModelId)}</span>
                      </div>
                      {diffRows.map((line, index) => (
                        <div key={`${line.type}-${index}`} className={`source-diff-row source-diff-${line.type}`}>
                          <div className="source-diff-cell source-diff-left-cell">
                            <span className="source-diff-gutter">{line.leftLine ?? ''}</span>
                            <span className="source-diff-marker">{line.type === 'removed' || line.type === 'changed' ? '-' : ' '}</span>
                            <code>{line.leftText || ' '}</code>
                          </div>
                          <div className="source-diff-cell source-diff-right-cell">
                            <span className="source-diff-gutter">{line.rightLine ?? ''}</span>
                            <span className="source-diff-marker">{line.type === 'added' || line.type === 'changed' ? '+' : ' '}</span>
                            <code>{line.rightText || ' '}</code>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ) : (
                <div className="source-editor-view">
                  <textarea
                    className="source-editor"
                    value={source}
                    onChange={(e) => setSource(e.target.value)}
                    spellCheck={false}
                    aria-label="Editable CadQuery source code"
                  />
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
