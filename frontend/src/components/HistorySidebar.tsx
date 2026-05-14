import React from 'react';
import { useProjectStore, useViewportStore } from '../stores';
import { formatLocalDateTime } from '../time';
import type { ModelInfo } from '../types';

interface HistorySidebarProps {
  versions: ModelInfo[];
  onSelect: (modelId: string) => void;
}

const HistorySidebar: React.FC<HistorySidebarProps> = ({ versions, onSelect }) => {
  const { currentModelId } = useViewportStore();
  const { project } = useProjectStore();

  if (!project) return null;

  return (
    <div className="history-sidebar">
      <div className="history-header">
        <h3>Design History</h3>
        <span className="history-count">{versions.length} versions</span>
      </div>
      
      <div className="history-list">
        {versions.length === 0 ? (
          <div className="history-empty">
            No versions generated yet.
          </div>
        ) : (
          [...versions].reverse().map((v) => (
            <div
              key={v.model_id}
              className={`history-item ${v.model_id === currentModelId ? 'active' : ''} ${v.failure_type ? 'failed' : ''}`}
              onClick={() => onSelect(v.model_id)}
            >
              <div className="history-item-top">
                <span className="history-id">#{v.model_id.slice(-4)}</span>
                <span className="history-time">{formatLocalDateTime(v.created_at)}</span>
              </div>
              
              <div className="history-prompt" title={v.prompt}>
                {v.prompt || (v.iteration ? `Repair iteration ${v.iteration}` : 'Checkpoint')}
              </div>

              {v.parent_model_id && (
                <div className="history-lineage" title={`Branched from #${v.parent_model_id}`}>
                  <span className="history-lineage-icon">↳</span>
                  <span className="history-lineage-label">from</span>
                  <span className="history-lineage-id">#{v.parent_model_id.slice(-4)}</span>
                </div>
              )}

              <div className="history-stats">
                {v.failure_type ? (
                  <span className="history-status failed">
                    ⚠️ {v.failure_type.replace('_', ' ')}
                  </span>
                ) : (
                  <span className="history-status success">
                    ✅ Success
                  </span>
                )}
                {v.vision_score !== undefined && v.vision_score !== null && (
                  <span className="history-score" title="Vision critique score">
                    👁️ {Math.round(v.vision_score * 100)}%
                  </span>
                )}
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
};

export default HistorySidebar;
