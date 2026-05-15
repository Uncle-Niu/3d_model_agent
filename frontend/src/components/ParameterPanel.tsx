/**
 * ParameterPanel - editable list of model parameters.
 */

import { useEffect, useState } from 'react';
import { api } from '../api';
import { useViewportStore } from '../stores';
import type { CadParameter, ModelInfo } from '../types';
import { toast } from './ui/Toast';

type ExecuteSourceResponse = {
  success: boolean;
  message: string;
  model: ModelInfo;
  glb_url?: string | null;
  violations: string[];
};

interface ParameterPanelProps {
  insideDock?: boolean;
}

function shortId(id: string, n = 6) {
  return id.length <= n ? id : id.slice(-n);
}

export default function ParameterPanel({ insideDock }: ParameterPanelProps = {}) {
  const { currentModelId, currentProjectId } = useViewportStore();
  const viewport = useViewportStore();
  const [isOpen, setIsOpen] = useState(true);
  const [parameters, setParameters] = useState<CadParameter[]>([]);
  const [localValues, setLocalValues] = useState<Record<string, any>>({});
  const [isLoading, setIsLoading] = useState(false);
  const [isUpdating, setIsUpdating] = useState(false);

  useEffect(() => {
    if (!currentProjectId || !currentModelId) {
      setParameters([]);
      setLocalValues({});
      return;
    }

    let cancelled = false;
    setIsLoading(true);

    api.get<CadParameter[]>(`/api/projects/${currentProjectId}/models/${currentModelId}/parameters`)
      .then((params) => {
        if (cancelled) return;
        setParameters(params);
        const values: Record<string, any> = {};
        params.forEach(p => { values[p.name] = p.value; });
        setLocalValues(values);
      })
      .catch((err) => {
        if (cancelled) return;
        toast.error('Failed to load parameters');
        console.error(err);
      })
      .finally(() => { if (!cancelled) setIsLoading(false); });

    return () => { cancelled = true; };
  }, [currentProjectId, currentModelId]);

  function handleChange(name: string, value: any, type: string) {
    let typedValue = value;
    if (type === 'float') typedValue = parseFloat(value);
    if (type === 'int') typedValue = parseInt(value, 10);
    if (type === 'bool') typedValue = value === 'true' || value === true;
    setLocalValues(prev => ({ ...prev, [name]: typedValue }));
  }

  async function handleUpdate() {
    if (!currentProjectId || !currentModelId) return;
    setIsUpdating(true);
    try {
      const result = await api.post<ExecuteSourceResponse>(
        `/api/projects/${currentProjectId}/models/${currentModelId}/update_parameters`,
        { parameters: localValues }
      );

      if (result.success) {
        toast.success(`Updated to #${shortId(result.model.model_id)}`);
        if (result.glb_url) {
          viewport.setModel(result.model.model_id, api.url(result.glb_url), currentProjectId);
        }
      } else {
        toast.error(`Update failed: ${result.message}`);
      }
    } catch (err) {
      toast.error(`Update failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setIsUpdating(false);
    }
  }

  function handleRevert() {
    const values: Record<string, any> = {};
    parameters.forEach(p => { values[p.name] = p.value; });
    setLocalValues(values);
  }

  const isDirty = parameters.some(p => localValues[p.name] !== p.value);

  if (!currentModelId) return null;

  const content = (
    <div className={insideDock ? "docked-panel-content" : "panel-content"}>
      {isLoading ? (
        <div className="loading">Loading…</div>
      ) : parameters.length === 0 ? (
        <div className="empty">No parameters detected in source.</div>
      ) : (
        <div className="parameter-list">
          {parameters.map(p => {
            const hasRange = p.min_value !== undefined && p.max_value !== undefined;
            return (
              <div key={p.name} className="parameter-item">
                <div className="parameter-label-row">
                  <label htmlFor={`param-${p.name}`}>{p.name}</label>
                  <span className="parameter-hint">
                    {p.type}
                    {hasRange ? ` · ${p.min_value}…${p.max_value}` : ''}
                  </span>
                </div>
                {p.type === 'bool' ? (
                  <select
                    id={`param-${p.name}`}
                    value={String(localValues[p.name])}
                    onChange={(e) => handleChange(p.name, e.target.value, p.type)}
                  >
                    <option value="true">True</option>
                    <option value="false">False</option>
                  </select>
                ) : (
                  <input
                    id={`param-${p.name}`}
                    type={p.type === 'str' ? 'text' : 'number'}
                    step={p.type === 'float' ? '0.1' : '1'}
                    min={p.min_value}
                    max={p.max_value}
                    value={localValues[p.name] ?? ''}
                    onChange={(e) => handleChange(p.name, e.target.value, p.type)}
                  />
                )}
                {p.description && <div className="parameter-desc">{p.description}</div>}
              </div>
            );
          })}

          <div className="panel-actions">
            {isDirty && (
              <button className="btn btn-ghost" onClick={handleRevert} disabled={isUpdating}>
                Revert
              </button>
            )}
            <button
              className="btn btn-primary"
              disabled={!isDirty || isUpdating}
              onClick={handleUpdate}
            >
              {isUpdating ? 'Updating…' : 'Update model'}
            </button>
          </div>
        </div>
      )}
    </div>
  );

  if (insideDock) return content;

  return (
    <div className={`parameter-panel ${isOpen ? 'open' : 'closed'}`}>
      <div className="panel-header" onClick={() => setIsOpen(!isOpen)}>
        <h3>Parameters</h3>
        <span className="toggle-icon">{isOpen ? '−' : '+'}</span>
      </div>
      {isOpen && content}
    </div>
  );
}
