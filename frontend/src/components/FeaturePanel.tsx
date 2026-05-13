/**
 * FeaturePanel - list of features (parts/named objects) in the model.
 */

import { useEffect, useState } from 'react';
import { api } from '../api';
import { useViewportStore, useSelectionStore } from '../stores';
import type { FeatureManifest } from '../types';

export default function FeaturePanel() {
  const { currentModelId, currentProjectId } = useViewportStore();
  const { selectedFeatureName, setSelection } = useSelectionStore();
  const [isOpen, setIsOpen] = useState(false);
  const [features, setFeatures] = useState<FeatureManifest[]>([]);
  const [isLoading, setIsLoading] = useState(false);

  useEffect(() => {
    if (!currentProjectId || !currentModelId) {
      setFeatures([]);
      return;
    }

    let cancelled = false;
    setIsLoading(true);

    api.get<FeatureManifest[]>(`/api/projects/${currentProjectId}/models/${currentModelId}/features`)
      .then((data) => {
        if (!cancelled) setFeatures(data);
      })
      .catch((err) => {
        console.error('Failed to load features:', err);
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [currentProjectId, currentModelId]);

  if (!currentModelId) return null;

  return (
    <div className={`feature-panel ${isOpen ? 'open' : 'closed'}`}>
      <div className="panel-header" onClick={() => setIsOpen(!isOpen)}>
        <h3>Features</h3>
        <span className="toggle-icon">{isOpen ? '−' : '+'}</span>
      </div>
      
      {isOpen && (
        <div className="panel-content">
          {isLoading ? (
            <div className="loading">Loading...</div>
          ) : features.length === 0 ? (
            <div className="empty">No features detected.</div>
          ) : (
            <ul className="feature-list">
              {features.map((f, i) => (
                <li 
                  key={`${f.name}-${i}`} 
                  className={`feature-item ${selectedFeatureName === f.name ? 'active' : ''}`}
                  onClick={() => setSelection(f.name === selectedFeatureName ? null : f.name)}
                >
                  <span className="feature-name">{f.name}</span>
                  <span className="feature-type">{f.type}</span>
                  <div className="feature-coords">
                    ({f.center[0].toFixed(1)}, {f.center[1].toFixed(1)}, {f.center[2].toFixed(1)})
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
