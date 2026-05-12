/**
 * SourcePanel - collapsible view of the current model's CadQuery source.
 */

import { useEffect, useState } from 'react';
import { api } from '../api';
import { useViewportStore } from '../stores';
import { formatLocalDateTime } from '../time';
import type { ModelInfo } from '../types';

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

function buildLineDiff(oldText: string, newText: string): DiffLine[] {
  const oldLines = oldText.split('\n');
  const newLines = newText.split('\n');
  const dp: number[][] = Array.from({ length: oldLines.length + 1 }, () =>
    Array(newLines.length + 1).fill(0)
  );

  for (let i = oldLines.length - 1; i >= 0; i -= 1) {
    for (let j = newLines.length - 1; j >= 0; j -= 1) {
      dp[i][j] = oldLines[i] === newLines[j]
        ? dp[i + 1][j + 1] + 1
        : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }

  const diff: DiffLine[] = [];
  let i = 0;
  let j = 0;
  let oldLine = 1;
  let newLine = 1;

  while (i < oldLines.length && j < newLines.length) {
    if (oldLines[i] === newLines[j]) {
      diff.push({ type: 'same', text: oldLines[i], oldLine, newLine });
      i += 1;
      j += 1;
      oldLine += 1;
      newLine += 1;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      diff.push({ type: 'removed', text: oldLines[i], oldLine });
      i += 1;
      oldLine += 1;
    } else {
      diff.push({ type: 'added', text: newLines[j], newLine });
      j += 1;
      newLine += 1;
    }
  }

  while (i < oldLines.length) {
    diff.push({ type: 'removed', text: oldLines[i], oldLine });
    i += 1;
    oldLine += 1;
  }

  while (j < newLines.length) {
    diff.push({ type: 'added', text: newLines[j], newLine });
    j += 1;
    newLine += 1;
  }

  return diff;
}

function buildSideBySideDiff(leftText: string, rightText: string): SideBySideDiffRow[] {
  const diff = buildLineDiff(leftText, rightText);
  const rows: SideBySideDiffRow[] = [];

  for (let i = 0; i < diff.length; i += 1) {
    const line = diff[i];
    const next = diff[i + 1];

    if (line.type === 'same') {
      rows.push({
        type: 'same',
        leftText: line.text,
        rightText: line.text,
        leftLine: line.oldLine,
        rightLine: line.newLine,
      });
    } else if (line.type === 'removed' && next?.type === 'added') {
      rows.push({
        type: 'changed',
        leftText: line.text,
        rightText: next.text,
        leftLine: line.oldLine,
        rightLine: next.newLine,
      });
      i += 1;
    } else if (line.type === 'removed') {
      rows.push({
        type: 'removed',
        leftText: line.text,
        rightText: '',
        leftLine: line.oldLine,
      });
    } else {
      rows.push({
        type: 'added',
        leftText: '',
        rightText: line.text,
        rightLine: line.newLine,
      });
    }
  }

  return rows;
}

export default function SourcePanel() {
  const { currentModelId, currentProjectId } = useViewportStore();
  const viewport = useViewportStore();
  const [isOpen, setIsOpen] = useState(false);
  const [source, setSource] = useState('');
  const [savedSource, setSavedSource] = useState('');
  const [modelVersions, setModelVersions] = useState<ModelInfo[]>([]);
  const [leftModelId, setLeftModelId] = useState('');
  const [rightModelId, setRightModelId] = useState('');
  const [leftSource, setLeftSource] = useState('');
  const [rightSource, setRightSource] = useState('');
  const [viewMode, setViewMode] = useState<'source' | 'diff'>('source');
  const [error, setError] = useState('');
  const [executionMessage, setExecutionMessage] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [isExecuting, setIsExecuting] = useState(false);
  const [panelHeight, setPanelHeight] = useState(() => {
    const stored = Number(localStorage.getItem('sourcePanelHeight'));
    return Number.isFinite(stored) && stored >= 220 ? stored : 360;
  });

  useEffect(() => {
    if (!isOpen || !currentProjectId || !currentModelId) return;

    let cancelled = false;
    setIsLoading(true);
    setError('');

    api.getText(`/api/projects/${currentProjectId}/models/${currentModelId}/source`)
      .then((text) => {
        if (!cancelled) {
          setSource(text);
          setSavedSource(text);
          setExecutionMessage('');
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setSource('');
          setError(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [isOpen, currentProjectId, currentModelId]);

  useEffect(() => {
    if (!isOpen || !currentProjectId) return;

    let cancelled = false;
    api.get<ModelInfo[]>(`/api/projects/${currentProjectId}/models`)
      .then((models) => {
        if (cancelled) return;
        const successful = models.filter((model) => model.has_glb);
        setModelVersions(successful);

        const currentIndex = successful.findIndex((model) => model.model_id === currentModelId);
        const previous = currentIndex > 0 ? successful[currentIndex - 1] : successful[0];
        setLeftModelId((existing) => {
          if (existing && successful.some((model) => model.model_id === existing)) return existing;
          if (previous && previous.model_id !== currentModelId) return previous.model_id;
          return successful.find((model) => model.model_id !== currentModelId)?.model_id ?? '';
        });
        setRightModelId((existing) => {
          if (existing && successful.some((model) => model.model_id === existing)) return existing;
          return currentModelId ?? successful.at(-1)?.model_id ?? '';
        });
      })
      .catch((err) => {
        console.error('Failed to load model versions for source diff:', err);
      });

    return () => {
      cancelled = true;
    };
  }, [isOpen, currentProjectId, currentModelId]);

  useEffect(() => {
    if (!isOpen || viewMode !== 'diff' || !currentProjectId || !leftModelId) return;

    let cancelled = false;
    if (leftModelId === currentModelId) {
      setLeftSource(source);
      return;
    }

    api.getText(`/api/projects/${currentProjectId}/models/${leftModelId}/source`)
      .then((text) => {
        if (!cancelled) setLeftSource(text);
      })
      .catch((err) => {
        if (!cancelled) {
          setLeftSource('');
          setError(err instanceof Error ? err.message : String(err));
        }
      });

    return () => {
      cancelled = true;
    };
  }, [isOpen, viewMode, currentProjectId, currentModelId, leftModelId, source]);

  useEffect(() => {
    if (!isOpen || viewMode !== 'diff' || !currentProjectId || !rightModelId) return;

    let cancelled = false;
    if (rightModelId === currentModelId) {
      setRightSource(source);
      return;
    }

    api.getText(`/api/projects/${currentProjectId}/models/${rightModelId}/source`)
      .then((text) => {
        if (!cancelled) setRightSource(text);
      })
      .catch((err) => {
        if (!cancelled) {
          setRightSource('');
          setError(err instanceof Error ? err.message : String(err));
        }
      });

    return () => {
      cancelled = true;
    };
  }, [isOpen, viewMode, currentProjectId, currentModelId, rightModelId, source]);

  async function copySource() {
    if (!source) return;
    await navigator.clipboard.writeText(source);
  }

  async function executeSource() {
    if (!currentProjectId || !source.trim()) return;

    setIsExecuting(true);
    setError('');
    setExecutionMessage('');

    try {
      const result = await api.post<ExecuteSourceResponse>(
        `/api/projects/${currentProjectId}/models/execute_source`,
        {
          source,
          prompt: currentModelId
            ? `Manual edit from ${currentModelId}`
            : 'Manual source edit',
        }
      );

      if (!result.success) {
        setExecutionMessage(`Execution failed: ${result.message}`);
        return;
      }

      setSavedSource(source);
      setExecutionMessage(`Saved and executed as ${result.model.model_id}.`);
      if (result.glb_url) {
        viewport.setModel(result.model.model_id, api.url(result.glb_url), currentProjectId);
      }
      window.dispatchEvent(new CustomEvent('cad-model-ready', {
        detail: { projectId: currentProjectId, modelId: result.model.model_id },
      }));
    } catch (err) {
      setExecutionMessage(`Execution failed: ${err instanceof Error ? err.message : String(err)}`);
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

  const label = currentModelId ? `Source CAD Code - ${currentModelId}` : 'Source CAD Code';
  const diffRows = buildSideBySideDiff(leftSource, rightSource);
  const addedCount = diffRows.filter((line) => line.type === 'added' || line.type === 'changed').length;
  const removedCount = diffRows.filter((line) => line.type === 'removed' || line.type === 'changed').length;
  const isDirty = source !== savedSource;

  return (
    <div
      className={`source-panel ${isOpen ? 'source-panel-open' : ''}`}
      style={isOpen ? { height: panelHeight } : undefined}
    >
      {isOpen && (
        <div
          className="source-resize-handle"
          onPointerDown={handleResizeStart}
          title="Drag to resize source panel"
        />
      )}
      <div className="source-toggle-bar" onClick={() => setIsOpen((open) => !open)}>
        <span className="source-toggle-icon">{isOpen ? 'v' : '>'}</span>
        <span className="source-toggle-label">{label}</span>
        {isOpen && source && (
          <div className="source-toolbar" onClick={(e) => e.stopPropagation()}>
            <button
              type="button"
              className={viewMode === 'source' ? 'active' : ''}
              onClick={() => setViewMode('source')}
            >
              Source
            </button>
            <button
              type="button"
              className={viewMode === 'diff' ? 'active' : ''}
              onClick={() => setViewMode('diff')}
            >
              Diff
            </button>
            <button type="button" onClick={copySource}>Copy</button>
            <button
              type="button"
              onClick={executeSource}
              disabled={isExecuting || !source.trim()}
            >
              {isExecuting ? 'Executing...' : isDirty ? 'Save & Execute' : 'Execute'}
            </button>
          </div>
        )}
      </div>

      {isOpen && (
        <div className="source-content">
          {!currentModelId || !currentProjectId ? (
            <div className="source-empty">No generated model selected.</div>
          ) : isLoading ? (
            <div className="source-empty">Loading source...</div>
          ) : error ? (
            <div className="source-error">{error}</div>
          ) : viewMode === 'diff' ? (
            <div className="source-diff-view">
              <div className="source-diff-controls">
                <span>Left</span>
                <select
                  value={leftModelId}
                  onChange={(e) => setLeftModelId(e.target.value)}
                  aria-label="Select left source version"
                >
                  {modelVersions.map((model) => (
                    <option key={model.model_id} value={model.model_id}>
                      {model.model_id} - {formatLocalDateTime(model.created_at)} - {model.prompt || 'checkpoint'}
                    </option>
                  ))}
                </select>
                <span>Right</span>
                <select
                  value={rightModelId}
                  onChange={(e) => setRightModelId(e.target.value)}
                  aria-label="Select right source version"
                >
                  {modelVersions.map((model) => (
                    <option key={model.model_id} value={model.model_id}>
                      {model.model_id} - {formatLocalDateTime(model.created_at)} - {model.prompt || 'checkpoint'}
                    </option>
                  ))}
                </select>
                <span className="source-diff-stats">
                  +{addedCount} / -{removedCount}
                </span>
              </div>
              {!leftModelId || !rightModelId ? (
                <div className="source-empty">Pick left and right model versions to compare.</div>
              ) : (
                <div className="source-diff-side-by-side">
                  <div className="source-diff-heading">
                    <span>{leftModelId}</span>
                    <span>{rightModelId}</span>
                  </div>
                  {diffRows.map((line, index) => (
                    <div key={`${line.type}-${index}`} className={`source-diff-row source-diff-${line.type}`}>
                      <div className="source-diff-cell source-diff-left-cell">
                        <span className="source-diff-gutter">{line.leftLine ?? ''}</span>
                        <span className="source-diff-marker">
                          {line.type === 'removed' || line.type === 'changed' ? '-' : ' '}
                        </span>
                        <code>{line.leftText || ' '}</code>
                      </div>
                      <div className="source-diff-cell source-diff-right-cell">
                        <span className="source-diff-gutter">{line.rightLine ?? ''}</span>
                        <span className="source-diff-marker">
                          {line.type === 'added' || line.type === 'changed' ? '+' : ' '}
                        </span>
                        <code>{line.rightText || ' '}</code>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ) : (
            <div className="source-editor-view">
              {executionMessage && (
                <div className={executionMessage.startsWith('Execution failed') ? 'source-error compact' : 'source-status'}>
                  {executionMessage}
                </div>
              )}
              <textarea
                className="source-editor"
                value={source}
                onChange={(e) => setSource(e.target.value)}
                spellCheck={false}
                aria-label="Editable CadQuery source code"
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}
