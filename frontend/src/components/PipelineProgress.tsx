/**
 * PipelineProgress — multi-level timeline of an agent turn's progress.
 *
 * Rendering tiers:
 *   - Collapsed (default for persisted history): one line — "Verified in 3 steps"
 *     with a "Show details" toggle.
 *   - Expanded: vertical timeline of stages. Each stage shows its icon, name,
 *     and headline message. Clicking a stage reveals its rationale and the
 *     concrete inputs the agent drew on, written as prose instead of cryptic
 *     "WHY / USED / SKIPPED" labels.
 *   - During a live run (isLive=true) the timeline is expanded by default and
 *     auto-expands the most recently emitted step so the user can follow what
 *     the agent is doing right now.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import type { PipelineStep } from '../types';

interface PipelineProgressProps {
  steps: PipelineStep[];
  isLive?: boolean;
}

const STAGE_META: Record<string, { icon: string; label: string; tone: 'neutral' | 'good' | 'bad' | 'warn' }> = {
  planning:     { icon: '◴', label: 'Planning',     tone: 'neutral' },
  researching:  { icon: '⌕', label: 'Researching',  tone: 'neutral' },
  generating:   { icon: '✦', label: 'Generating',   tone: 'neutral' },
  executing:    { icon: '▶', label: 'Executing',    tone: 'neutral' },
  tessellating: { icon: '◆', label: 'Tessellating', tone: 'neutral' },
  rendering:    { icon: '◐', label: 'Rendering',    tone: 'neutral' },
  critiquing:   { icon: '◉', label: 'Verifying',    tone: 'neutral' },
  validating:   { icon: '✓', label: 'Validating',   tone: 'good'    },
  repairing:    { icon: '↻', label: 'Repairing',    tone: 'warn'    },
  failed:       { icon: '✕', label: 'Failed',       tone: 'bad'     },
};

function stageMeta(stage: string) {
  return STAGE_META[stage] ?? { icon: '·', label: stage, tone: 'neutral' as const };
}

/**
 * Convert legacy data fields (why/used/skipped) into prose sentences. Returns
 * an array of strings to render — empty if there's nothing to say.
 */
function dataAsProse(data: PipelineStep['data']): string[] {
  if (!data) return [];
  const out: string[] = [];
  const rationale = data.rationale || data.why;
  if (typeof rationale === 'string' && rationale.trim()) {
    out.push(rationale.trim());
  }
  if (Array.isArray(data.used) && data.used.length > 0) {
    out.push(`Used inputs: ${joinNicely(data.used)}.`);
  }
  if (Array.isArray(data.skipped) && data.skipped.length > 0) {
    out.push(`Skipped: ${joinNicely(data.skipped)}.`);
  }
  return out;
}

function joinNicely(items: string[]): string {
  if (items.length === 1) return items[0];
  if (items.length === 2) return `${items[0]} and ${items[1]}`;
  return `${items.slice(0, -1).join(', ')}, and ${items.slice(-1)[0]}`;
}

/**
 * Render the special "planning" payload (components / key features) as a
 * compact list when present, so the user can see what the agent committed to.
 */
function PlanArtifacts({ data }: { data: PipelineStep['data'] }) {
  if (!data) return null;
  const components = Array.isArray(data.components) ? (data.components as Array<Record<string, unknown>>) : null;
  const features = Array.isArray(data.key_features) ? (data.key_features as string[]) : null;
  const reasoning = typeof data.raw_reasoning === 'string' ? data.raw_reasoning : null;

  if (!components?.length && !features?.length && !reasoning) return null;

  return (
    <div className="pipeline-artifact">
      {reasoning && !components?.length && (
        <div className="pipeline-artifact-block">
          <div className="pipeline-artifact-title">Agent Reasoning</div>
          <div className="pipeline-step-message-full">{reasoning}</div>
        </div>
      )}
      {components && components.length > 0 && (
        <div className="pipeline-artifact-block">
          <div className="pipeline-artifact-title">Components</div>
          <ul className="pipeline-artifact-list">
            {components.map((c, i) => (
              <li key={i}>
                <span className="pipeline-artifact-name">{String(c.name ?? `part ${i + 1}`)}</span>
                {typeof c.primitive === 'string' && (
                  <span className="pipeline-artifact-meta"> — {c.primitive}</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      {features && features.length > 0 && (
        <div className="pipeline-artifact-block">
          <div className="pipeline-artifact-title">Key features the result must show</div>
          <ul className="pipeline-artifact-list">
            {features.map((f, i) => <li key={i}>{f}</li>)}
          </ul>
        </div>
      )}
    </div>
  );
}

function ResearchArtifacts({ data }: { data: PipelineStep['data'] }) {
  if (!data) return null;
  const results = Array.isArray(data.research_results) ? (data.research_results as Array<Record<string, string>>) : null;

  if (!results?.length) return null;

  return (
    <div className="pipeline-artifact">
      <div className="pipeline-artifact-block">
        <div className="pipeline-artifact-title">Search Results</div>
        <ul className="pipeline-artifact-list">
          {results.map((r, i) => (
            <li key={i}>
              <a href={r.url} target="_blank" rel="noreferrer" className="pipeline-artifact-link">
                {r.title || new URL(r.url).hostname}
              </a>
              {r.snippet && <div className="pipeline-artifact-snippet">{r.snippet}</div>}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

interface PipelineStepRowProps {
  step: PipelineStep;
  index: number;
  isLast: boolean;
  isExpanded: boolean;
  onToggle: () => void;
}

function PipelineStepRow({ step, isLast, isExpanded, onToggle }: PipelineStepRowProps) {
  const meta = stageMeta(step.stage);
  
  // Truncate long messages for the headline to keep the timeline clean
  const maxHeadlineLength = 160;
  const lines = (step.message || '').split('\n').filter(l => l.trim());
  const firstLine = lines[0] || '';
  const isLong = firstLine.length > maxHeadlineLength || lines.length > 1;
  
  const displayMessage = isLong 
    ? firstLine.slice(0, maxHeadlineLength).trim() + '...' 
    : firstLine;

  const data = step.data || {};
  const rationale = typeof data.rationale === 'string' ? data.rationale : null;
  const outcome = typeof data.outcome === 'string' ? data.outcome : null;
  const usedInputs = Array.isArray(data.used) ? data.used : null;
  const skipped = Array.isArray(data.skipped) ? data.skipped : null;

  const hasDetails = !!(step.details || rationale || outcome || usedInputs || skipped || isLong || step.data?.raw_reasoning || step.data?.research_results);
  const showPlan = step.stage === 'planning' && step.data && (
    Array.isArray(step.data.components) || 
    Array.isArray(step.data.key_features) ||
    typeof step.data.raw_reasoning === 'string'
  );
  const showResearch = step.stage === 'researching' && step.data && Array.isArray(step.data.research_results);

  return (
    <div className={`pipeline-step pipeline-step-tone-${meta.tone}`}>
      <div className="pipeline-step-marker">
        <div className="pipeline-step-icon" title={meta.label}>{meta.icon}</div>
        {!isLast && <div className="pipeline-step-line" />}
      </div>

      <div className="pipeline-step-content">
        <button
          type="button"
          className="pipeline-step-headline"
          onClick={hasDetails ? onToggle : undefined}
          aria-expanded={isExpanded}
          disabled={!hasDetails}
        >
          <span className="pipeline-step-stage">{meta.label}</span>
          <span className="pipeline-step-message">{displayMessage}</span>
          {hasDetails && (
            <span className="pipeline-step-chevron" aria-hidden="true">{isExpanded ? '▾' : '▸'}</span>
          )}
        </button>

        {isExpanded && hasDetails && (
          <div className="pipeline-step-body">
            {/* 1. Hardcoded Description + Dynamic Rationale combined */}
            {(step.details || rationale) && (
              <p className="pipeline-step-combined">
                {step.details && <span className="pipeline-step-hardcoded">{step.details} </span>}
                {rationale && <span className="pipeline-step-rationale">{rationale}</span>}
              </p>
            )}

            {/* 2. Clear Decision / Outcome */}
            {outcome && (
              <p className="pipeline-step-outcome">
                <span className="outcome-label">Result:</span> {outcome}
              </p>
            )}

            {/* 3. Full message (if it was truncated) */}
            {isLong && (
              <div className="pipeline-step-message-full">
                {step.message}
              </div>
            )}

            {/* 4. Inputs / Context */}
            {(usedInputs || skipped) && (
              <div className="pipeline-step-context">
                {usedInputs && usedInputs.length > 0 && (
                  <p className="pipeline-step-used">Used inputs: {joinNicely(usedInputs)}</p>
                )}
                {skipped && skipped.length > 0 && (
                  <p className="pipeline-step-skipped">Skipped: {joinNicely(skipped)}</p>
                )}
              </div>
            )}

            {showPlan && <PlanArtifacts data={step.data} />}
            {showResearch && <ResearchArtifacts data={step.data} />}
          </div>
        )}
      </div>
    </div>
  );
}

export default function PipelineProgress({ steps, isLive = false }: PipelineProgressProps) {
  const [showTimeline, setShowTimeline] = useState(isLive);
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set());
  const lastSeenRef = useRef<number>(0);

  // Auto-expand newly arriving steps while the run is live, so the user can
  // follow what the agent is doing without clicking each one.
  useEffect(() => {
    if (!isLive) return;
    if (steps.length > lastSeenRef.current) {
      setExpanded((prev) => {
        const next = new Set(prev);
        // Only expand the latest arrival — older steps stay at whatever the
        // user left them. This keeps the live view focused without wiping
        // the user's manual collapses.
        next.add(steps.length - 1);
        return next;
      });
      lastSeenRef.current = steps.length;
    }
  }, [steps.length, isLive]);

  const headline = useMemo(() => {
    if (steps.length === 0) return null;
    const latest = steps[steps.length - 1];
    const meta = stageMeta(latest.stage);
    if (isLive) return `${meta.icon} ${meta.label} — ${latest.message}`;
    const repairs = steps.filter((s) => s.stage === 'repairing').length;
    if (repairs > 0) return `Completed in ${steps.length} steps · ${repairs} repair${repairs === 1 ? '' : 's'}`;
    return `Completed in ${steps.length} steps`;
  }, [steps, isLive]);

  const expandables = useMemo(() => {
    return steps
      .map((step, i) => {
        const prose = dataAsProse(step.data);
        const hasDetails = !!(step.details || prose.length > 0 || step.data?.outcome || step.data);
        return hasDetails ? i : -1;
      })
      .filter((i) => i !== -1);
  }, [steps]);

  const allExpanded = expandables.length > 0 && expandables.every((i) => expanded.has(i));

  function toggleAllExpanded() {
    if (allExpanded) {
      setExpanded(new Set());
    } else {
      setExpanded(new Set(expandables));
    }
  }

  function toggleStep(i: number) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(i)) next.delete(i); else next.add(i);
      return next;
    });
  }

  if (steps.length === 0) return null;

  return (
    <div className={`pipeline-progress ${isLive ? 'is-live' : 'is-persisted'}`}>
      <div className="pipeline-header">
        <div className="pipeline-summary">
          {isLive && <span className="pipeline-live-pulse" aria-hidden="true" />}
          <span className="pipeline-headline">{headline}</span>
        </div>
        <div className="pipeline-actions">
          {showTimeline && expandables.length > 0 && (
            <button
              className="pipeline-action-btn"
              type="button"
              onClick={toggleAllExpanded}
            >
              {allExpanded ? 'Collapse all' : 'Expand all'}
            </button>
          )}
          <button
            className="pipeline-toggle-btn"
            type="button"
            onClick={() => setShowTimeline((v) => !v)}
          >
            {showTimeline ? 'Hide timeline' : 'Show timeline'}
          </button>
        </div>
      </div>

      {showTimeline && (
        <div className="pipeline-timeline">
          {steps.map((step, i) => (
            <PipelineStepRow
              key={`${step.stage}-${i}`}
              step={step}
              index={i}
              isLast={i === steps.length - 1}
              isExpanded={expanded.has(i)}
              onToggle={() => toggleStep(i)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
