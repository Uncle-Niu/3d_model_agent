/**
 * AssemblyPanel - Manage assembly parts, visibility, and exploded view.
 */

import { useEffect, useState } from 'react';
import { api } from '../api';
import { useViewportStore, useSelectionStore, useAssemblyStore } from '../stores';
import type { AssemblyManifest } from '../types';

interface AssemblyPanelProps {
  insideDock?: boolean;
}

export default function AssemblyPanel({ insideDock }: AssemblyPanelProps = {}) {
  const { currentModelId, currentProjectId } = useViewportStore();
  const { selectedFeatureName, setSelection } = useSelectionStore();
  const { partsVisibility, toggleVisibility, explodedFactor, setExplodedFactor, setParts } = useAssemblyStore();
  
  const [isOpen, setIsOpen] = useState(false);
  const [manifest, setManifest] = useState<AssemblyManifest | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  useEffect(() => {
    if (!currentProjectId || !currentModelId) {
      setManifest(null);
      return;
    }

    let cancelled = false;
    setIsLoading(true);

    api.get<AssemblyManifest>(`/api/projects/${currentProjectId}/models/${currentModelId}/assembly`)
      .then((data) => {
        if (!cancelled) {
          setManifest(data);
          // Initialize visibility store with part names
          if (data && data.parts) {
            setParts(data.parts.map(p => p.name));
          }
        }
      })
      .catch((err) => {
        console.error('Failed to load assembly manifest:', err);
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [currentProjectId, currentModelId, setParts]);

  async function downloadPart(partName: string, format: 'stl' | 'step') {
    if (!currentProjectId || !currentModelId) return;
    try {
      await api.downloadFile(
        `/api/projects/${currentProjectId}/models/${currentModelId}/assembly/${partName}/${format}`,
        `${partName}.${format}`
      );
    } catch (err) {
      console.error(`Failed to download ${format.toUpperCase()}:`, err);
      alert(`Failed to download ${format.toUpperCase()} for ${partName}`);
    }
  }

  if (!currentModelId) return null;

  const content = (
    <div className={insideDock ? "docked-panel-content" : "panel-content"}>
          <div className="exploded-view-control">
            <label>Exploded View</label>
            <input 
              type="range" 
              min="0" 
              max="2" 
              step="0.1" 
              value={explodedFactor} 
              onChange={(e) => setExplodedFactor(parseFloat(e.target.value))}
            />
            <span className="factor-value">{explodedFactor.toFixed(1)}x</span>
          </div>

          {isLoading ? (
            <div className="loading">Loading...</div>
          ) : !manifest || manifest.parts.length === 0 ? (
            <div className="empty">No parts detected.</div>
          ) : (
            <ul className="part-list">
              {manifest.parts.map((part, i) => (
                <li 
                  key={`${part.name}-${i}`} 
                  className={`part-item ${selectedFeatureName === part.name ? 'active' : ''}`}
                >
                  <div className="part-info" onClick={() => setSelection(part.name === selectedFeatureName ? null : part.name)}>
                    <span className="part-name" title={part.name}>{part.name}</span>
                    {part.geometry_stats && (
                      <span className="part-mass">{(part.geometry_stats.estimated_mass_g || 0).toFixed(1)}g</span>
                    )}
                  </div>
                  
                  <div className="part-actions">
                    <button 
                      className="part-action-btn"
                      onClick={(e) => { e.stopPropagation(); downloadPart(part.name, 'stl'); }}
                      title="Download STL"
                    >
                      STL
                    </button>
                    <button 
                      className="part-action-btn"
                      onClick={(e) => { e.stopPropagation(); downloadPart(part.name, 'step'); }}
                      title="Download STEP"
                    >
                      STP
                    </button>
                    <button 
                      className={`visibility-toggle ${partsVisibility[part.name] === false ? 'hidden' : 'visible'}`}
                      onClick={(e) => {
                        e.stopPropagation();
                        toggleVisibility(part.name);
                      }}
                      title={partsVisibility[part.name] === false ? "Show Part" : "Hide Part"}
                    >
                      {partsVisibility[part.name] === false ? '👁️‍🗨️' : '👁️'}
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
    </div>
  );

  if (insideDock) {
    return content;
  }

  return (
    <div className={`assembly-panel ${isOpen ? 'open' : 'closed'}`}>
      <div className="panel-header" onClick={() => setIsOpen(!isOpen)}>
        <h3>Assembly</h3>
        <span className="toggle-icon">{isOpen ? '−' : '+'}</span>
      </div>
      {isOpen && content}
    </div>
  );
}
