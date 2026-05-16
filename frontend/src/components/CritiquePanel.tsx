/**
 * CritiquePanel — vision critique results, rendered inline inside the
 * assistant message that produced it.
 */

import { useState } from 'react';
import { api } from '../api';
import { useChatStore, useCritiqueStore } from '../stores';

const SEVERITY_COLOR: Record<string, string> = {
  error: 'var(--critique-error)',
  warning: 'var(--critique-warning)',
  info: 'var(--critique-info)',
};

const SEVERITY_LABEL: Record<string, string> = {
  error: '✕ Error',
  warning: '⚠ Warning',
  info: '● Info',
};

export default function CritiquePanel() {
  const [expandedView, setExpandedView] = useState<string | null>(null);
  const { critique, clearCritique } = useCritiqueStore();
  const isGenerating = useChatStore((s) => s.isGenerating);

  if (!critique) return null;

  const { score, matchesIntent, issues, renderUrls } = critique;

  const scoreColor =
    score >= 0.8 ? 'var(--critique-good)' :
    score >= 0.65 ? 'var(--critique-warn)' :
    'var(--critique-error)';

  // Only claim "Repairing…" when there's actually an active pipeline run.
  const scoreLabel =
    score >= 0.8 ? 'Good' :
    score >= 0.65 ? 'Needs improvement' :
    isGenerating ? 'Poor — repairing…' : 'Poor';

  const viewOrder = ['iso', 'front', 'right', 'top'] as const;
  const expandedUrl = expandedView ? renderUrls[expandedView] : null;

  function toggleExpandedView(view: string) {
    setExpandedView((current) => current === view ? null : view);
  }

  return (
    <div className="critique-panel">
      <div className="critique-header">
        <div className="critique-title">
          <span className="critique-icon" aria-hidden="true">◉</span>
          Vision Critique
        </div>
        <div className="critique-score" style={{ color: scoreColor }}>
          <span className="critique-score-value">{(score * 100).toFixed(0)}%</span>
          <span className="critique-score-label">{scoreLabel}</span>
        </div>
        <button className="critique-close" onClick={clearCritique} title="Dismiss">✕</button>
      </div>

      {!matchesIntent && (
        <div className="critique-intent-warning">
          ⚠ Model may not fully match your description
        </div>
      )}

      {Object.keys(renderUrls).length > 0 && (
        <>
          <div className="critique-renders">
            {viewOrder.map((view) =>
              renderUrls[view] ? (
                <button
                  key={view}
                  type="button"
                  className={`critique-render-thumb${expandedView === view ? ' is-expanded' : ''}`}
                  onClick={() => toggleExpandedView(view)}
                  aria-expanded={expandedView === view}
                  title={`${expandedView === view ? 'Hide' : 'Show'} larger ${view} render`}
                >
                  <img
                    src={api.url(renderUrls[view])}
                    alt={`${view.toUpperCase()} vision render thumbnail`}
                    className="critique-render-img"
                    onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }}
                  />
                  <span className="critique-render-label">{view.toUpperCase()}</span>
                </button>
              ) : null
            )}
          </div>

          {expandedUrl && (
            <div className="critique-render-expanded">
              <img
                src={api.url(expandedUrl)}
                alt={`${expandedView?.toUpperCase()} vision render expanded`}
                className="critique-render-expanded-img"
                onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }}
              />
              <span className="critique-render-expanded-label">{expandedView?.toUpperCase()}</span>
            </div>
          )}
        </>
      )}

      {issues.length > 0 ? (
        <div className="critique-issues">
          {issues.map((issue, i) => (
            <div key={i} className="critique-issue">
              <span
                className="critique-issue-badge"
                style={{ background: SEVERITY_COLOR[issue.severity] || 'var(--critique-info)' }}
              >
                {SEVERITY_LABEL[issue.severity] || issue.severity}
              </span>
              <span className="critique-issue-type">{issue.issue_type}</span>
              <span className="critique-issue-desc">{issue.description}</span>
              {issue.location_hint && (
                <span className="critique-issue-location">@ {issue.location_hint}</span>
              )}
            </div>
          ))}
        </div>
      ) : (
        <div className="critique-ok">✓ No printability issues detected</div>
      )}
    </div>
  );
}
