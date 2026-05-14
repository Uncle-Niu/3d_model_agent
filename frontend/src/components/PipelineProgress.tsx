/**
 * PipelineProgress component — renders a visual timeline of the generation steps.
 */

import { useState } from 'react';
import type { PipelineStep } from '../types';

interface PipelineProgressProps {
  steps: PipelineStep[];
  isLive?: boolean;
}

const STAGE_ICONS: Record<string, string> = {
  researching: '🔍',
  generating:  '✍️',
  executing:   '⚙️',
  tessellating:'🔺',
  rendering:   '📷',
  critiquing:  '👁',
  repairing:   '🔧',
  failed:      '❌',
  validating:  '✅',
};

export default function PipelineProgress({ steps, isLive = false }: PipelineProgressProps) {
  const [showDetails, setShowDetails] = useState(false);

  if (steps.length === 0) return null;

  return (
    <div className={`pipeline-progress ${isLive ? 'is-live' : 'is-persisted'}`}>
      <div className="pipeline-header">
        <div className="pipeline-summary">
          <span className="pipeline-count">{steps.length} steps</span>
          {isLive && <span className="pipeline-live-indicator">Processing...</span>}
        </div>
        <button 
          className="pipeline-toggle-btn"
          onClick={() => setShowDetails(!showDetails)}
        >
          {showDetails ? 'Hide Details' : 'Show Details'}
        </button>
      </div>

      <div className="pipeline-timeline">
        {steps.map((step, i) => (
          <div key={i} className="pipeline-step">
            <div className="pipeline-step-marker">
              <div className="pipeline-step-icon">
                {STAGE_ICONS[step.stage] || '⏳'}
              </div>
              {i < steps.length - 1 && <div className="pipeline-step-line" />}
            </div>
            
            <div className="pipeline-step-content">
              <div className="pipeline-step-main">
                <span className="pipeline-step-stage">{step.stage}</span>
                <span className="pipeline-step-message">{step.message}</span>
              </div>
              
              {showDetails && step.details && (
                <div className="pipeline-step-details">
                  {step.details}
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
