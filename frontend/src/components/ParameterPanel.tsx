/**
 * ParameterPanel - editable list of model parameters.
 */

import { useEffect, useState } from 'react';
import { api } from '../api';
import { useViewportStore } from '../stores';
import type { CadParameter, ModelInfo } from '../types';

type ExecuteSourceResponse = {
  success: boolean;
  message: string;
  model: ModelInfo;
  glb_url?: string | null;
  violations: string[];
};

export default function ParameterPanel() {
  const { currentModelId, currentProjectId } = useViewportStore();
  const viewport = useViewportStore();
  const [isOpen, setIsOpen] = useState(true);
  const [parameters, setParameters] = useState<CadParameter[]>([]);
  const [localValues, setLocalValues] = useState<Record<string, any>>({});
  const [isLoading, setIsLoading] = useState(false);
  const [isUpdating, setIsUpdating] = useState(false);
  const [error, setError] = useState('');
  const [status, setStatus] = useState('');

  useEffect(() => {
    if (!currentProjectId || !currentModelId) {
      setParameters([]);
      setLocalValues({});
      return;
    }

    let cancelled = false;
    setIsLoading(true);
    setError('');
    setStatus('');

    api.get<CadParameter[]>(`/api/projects/${currentProjectId}/models/${currentModelId}/parameters`)
      .then((params) => {
        if (!cancelled) {
          setParameters(params);
          const values: Record<string, any> = {};
          params.forEach(p => {
            values[p.name] = p.value;
          });
          setLocalValues(values);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError('Failed to load parameters');
          console.error(err);
        }
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [currentProjectId, currentModelId]);

  function handleChange(name: string, value: any, type: string) {
    let typedValue = value;
    if (type === 'float') typedValue = parseFloat(value);
    if (type === 'int') typedValue = parseInt(value, 10);
    if (type === 'bool') typedValue = value === 'true' || value === true;

    setLocalValues(prev => ({
      ...prev,
      [name]: typedValue
    }));
  }

  async function handleUpdate() {
    if (!currentProjectId || !currentModelId) return;

    setIsUpdating(true);
    setError('');
    setStatus('Updating model...');

    try {
      const result = await api.post<ExecuteSourceResponse>(
        `/api/projects/${currentProjectId}/models/${currentModelId}/update_parameters`,
        { parameters: localValues }
      );

      if (result.success) {
        setStatus(`Updated to ${result.model.model_id}`);
        if (result.glb_url) {
          viewport.setModel(result.model.model_id, api.url(result.glb_url), currentProjectId);
        }
      } else {
        setError(`Failed: ${result.message}`);
      }
    } catch (err) {
      setError(`Error: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setIsUpdating(false);
    }
  }

  const isDirty = parameters.some(p => localValues[p.name] !== p.value);

  if (!currentModelId) return null;

  return (
    <div className={`parameter-panel ${isOpen ? 'open' : 'closed'}`}>
      <div className="panel-header" onClick={() => setIsOpen(!isOpen)}>
        <h3>Parameters</h3>
        <span className="toggle-icon">{isOpen ? '−' : '+'}</span>
      </div>
      
      {isOpen && (
        <div className="panel-content">
          {isLoading ? (
            <div className="loading">Loading...</div>
          ) : parameters.length === 0 ? (
            <div className="empty">No parameters detected in source.</div>
          ) : (
            <div className="parameter-list">
              {parameters.map(p => (
                <div key={p.name} className="parameter-item">
                  <label htmlFor={`param-${p.name}`}>{p.name}</label>
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
                      value={localValues[p.name] ?? ''}
                      onChange={(e) => handleChange(p.name, e.target.value, p.type)}
                    />
                  )}
                </div>
              ))}
              
              <div className="panel-actions">
                <button 
                  className="update-button"
                  disabled={!isDirty || isUpdating}
                  onClick={handleUpdate}
                >
                  {isUpdating ? 'Updating...' : 'Update Model'}
                </button>
              </div>
              
              {status && <div className="status-msg">{status}</div>}
              {error && <div className="error-msg">{error}</div>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
