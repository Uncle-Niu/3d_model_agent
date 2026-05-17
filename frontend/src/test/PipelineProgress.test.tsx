import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, beforeEach } from 'vitest';
import PipelineProgress from '../components/PipelineProgress';
import { useChatStore } from '../stores';
import type { PipelineStep } from '../types';

beforeEach(() => {
  useChatStore.getState().reset();
});

describe('PipelineProgress', () => {
  it('surfaces first-draft plan content before plan_ready exists', () => {
    const steps: PipelineStep[] = [{
      stage: 'planning',
      message: 'First-draft plan · 1 component',
      timestamp: '2026-05-15T12:00:00.000Z',
      data: {
        sub_stage: 'plan_draft',
        summary: '',
        raw_reasoning: 'Chosen flat print orientation with a front lip.',
        components: [{
          name: 'front_lip',
          description: 'raised lip that keeps the phone from sliding',
          primitive: 'box',
          dimensions: { width: 80, height: 8 },
          operation: 'union',
        }],
        key_features: ['front retaining lip'],
      },
    }];

    render(<PipelineProgress steps={steps} isLive={false} />);

    expect(screen.getByText('Design plan')).toBeInTheDocument();
    expect(screen.getByText('front_lip')).toBeInTheDocument();
    expect(screen.getByText('front retaining lip')).toBeInTheDocument();
  });

  it('shows raw planner reasoning for an empty draft when expanded', () => {
    const steps: PipelineStep[] = [{
      stage: 'planning',
      message: 'First-draft plan · (no summary)',
      timestamp: '2026-05-15T12:00:00.000Z',
      data: {
        sub_stage: 'plan_draft',
        summary: '',
        raw_reasoning: 'The planner response only contained free-form reasoning.',
      },
    }];

    render(<PipelineProgress steps={steps} isLive={true} />);

    expect(screen.getAllByText('Planner did not return a structured plan.').length).toBeGreaterThan(0);
    fireEvent.click(screen.getAllByText("Show planner's raw reasoning")[0]);
    expect(screen.getAllByText('The planner response only contained free-form reasoning.').length).toBeGreaterThan(0);
  });

  it('keeps the active repair status inside the timeline row', () => {
    const steps: PipelineStep[] = [{
      stage: 'repairing',
      message: 'Repairing syntax error (syntax attempt 3/8) · `model-003`',
      timestamp: '2026-05-15T12:00:00.000Z',
      data: {
        iteration: 3,
        failure_type: 'syntax error',
        model_id: 'model-003',
      },
    }];

    const { container } = render(<PipelineProgress steps={steps} isLive={true} />);

    expect(container.querySelector('.pipeline-headline')?.textContent).toMatch(/In progress/);
    expect(container.querySelector('.pipeline-headline')?.textContent).not.toContain('Repairing syntax error');
    expect(screen.getByText('Repairing syntax error (syntax attempt 3/8) · `model-003`')).toBeInTheDocument();
  });

  it('hoists the vision verifier card with thumbnails when a critiquing step carries renders', () => {
    const steps: PipelineStep[] = [{
      stage: 'critiquing',
      message: 'Vision score 0.40 · ✗ does NOT match intent · 4 error(s), 1 warning(s)',
      timestamp: '2026-05-15T12:00:00.000Z',
      data: {
        iteration: 4,
        vision_score: 0.4,
        matches_intent: false,
        vision_issues: [
          { severity: 'error', issue_type: 'missing_feature', description: 'lip missing' },
        ],
        render_urls: {
          iso: '/api/projects/p1/models/m1/renders/iso',
          front: '/api/projects/p1/models/m1/renders/front',
        },
      },
    }];

    const { container } = render(<PipelineProgress steps={steps} defaultShowTimeline={false} />);

    expect(screen.getByText('Vision verifier')).toBeInTheDocument();
    // Thumbnails for ISO + FRONT are present (label text inside the buttons).
    const thumbButtons = container.querySelectorAll('.critique-render-thumb');
    expect(thumbButtons.length).toBe(2);
    // Expand on click — clicking the first thumb should mount the larger image.
    fireEvent.click(thumbButtons[0]);
    expect(container.querySelector('.critique-render-expanded')).toBeTruthy();
  });

  it('normalizes legacy vision preflight and keeps critique inside its iteration', () => {
    const steps: PipelineStep[] = [
      {
        stage: 'critiquing',
        message: 'Checking the vision model with a smoke-test image.',
        timestamp: '2026-05-15T12:00:00.000Z',
        data: { sub_stage: 'vision_smoke_test' },
      },
      {
        stage: 'generating',
        message: 'Writing CadQuery code...',
        timestamp: '2026-05-15T12:00:00.500Z',
        data: { iteration: 1, in_progress: true },
      },
      {
        stage: 'rendering',
        message: 'Rendering ISO / Top / Front / Side views...',
        timestamp: '2026-05-15T12:00:00.750Z',
        data: { in_progress: true },
      },
      {
        stage: 'critiquing',
        message: 'Vision verifier reviewing the renders...',
        timestamp: '2026-05-15T12:00:01.000Z',
        data: { in_progress: true },
      },
    ];

    render(<PipelineProgress steps={steps} defaultShowTimeline={true} />);

    expect(screen.getByText('Preflight')).toBeInTheDocument();
    expect(screen.getByText('Vision smoke test')).toBeInTheDocument();
    expect(screen.getByText('Iteration 1')).toBeInTheDocument();
    expect(screen.getByText('Vision verifier reviewing the renders...')).toBeInTheDocument();
  });
});
