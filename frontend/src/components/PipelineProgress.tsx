/**
 * PipelineProgress component - renders a compact timeline of generation steps.
 */

import { useState } from 'react';
import type { PipelineStep } from '../types';

interface PipelineProgressProps {
  steps: PipelineStep[];
  isLive?: boolean;
}

const STAGE_ICONS: Record<string, string> = {
  planning: '◴',
  researching: '⌕',
  generating: '✦',
  executing: '▶',
  tessellating: '◆',
  rendering: '◐',
  critiquing: '◉',
  repairing: '↻',
  failed: '✕',
  validating: '✓',
};

function renderList(label: string, values?: string[]) {
  if (!values || values.length === 0) return null;

  return (
    <div className="pipeline-detail-row">
      <span className="pipeline-detail-label">{label}</span>
      <ul className="pipeline-detail-list">
        {values.map((value, i) => <li key={i}>{value}</li>)}
      </ul>
    </div>
  );
}

export default function PipelineProgress({ steps, isLive = false }: PipelineProgressProps) {
  const [showDetails, setShowDetails] = useState(isLive);

  if (steps.length === 0) return null;

  const latestStep = steps[steps.length - 1];
  const showTimeline = isLive || showDetails;

  return (
    <div className={`pipeline-progress ${isLive ? 'is-live' : 'is-persisted'}`}>
      <div className="pipeline-header">
        <div className="pipeline-summary">
          <span className="pipeline-count">{steps.length} steps</span>
          {isLive && <span className="pipeline-live-indicator">Processing...</span>}
          {!isLive && latestStep && (
            <span className="pipeline-latest-step">{latestStep.stage}: {latestStep.message}</span>
          )}
        </div>
        <button
          className="pipeline-toggle-btn"
          type="button"
          onClick={() => setShowDetails(!showDetails)}
        >
          {showDetails ? 'Hide progress' : 'Show progress'}
        </button>
      </div>

      {showTimeline && (
        <div className="pipeline-timeline">
          {steps.map((step, i) => (
            <div key={i} className="pipeline-step">
              <div className="pipeline-step-marker">
                <div className="pipeline-step-icon">
                  {STAGE_ICONS[step.stage] || '?'}
                </div>
                {i < steps.length - 1 && <div className="pipeline-step-line" />}
              </div>

              <div className="pipeline-step-content">
                <div className="pipeline-step-main">
                  <span className="pipeline-step-stage">{step.stage}</span>
                  <span className="pipeline-step-message">{step.message}</span>
                </div>

                {showDetails && (step.details || step.data) && (
                  <div className="pipeline-step-details">
                    {step.details && <p>{step.details}</p>}
                    {step.data?.why && (
                      <div className="pipeline-detail-row">
                        <span className="pipeline-detail-label">Why</span>
                        <p>{step.data.why}</p>
                      </div>
                    )}
                    {renderList('Used', step.data?.used)}
                    {renderList('Skipped', step.data?.skipped)}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
