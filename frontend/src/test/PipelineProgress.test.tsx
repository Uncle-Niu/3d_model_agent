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
      message: 'Repairing syntax error (attempt 3/5) · `model-003`',
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
    expect(screen.getByText('Repairing syntax error (attempt 3/5) · `model-003`')).toBeInTheDocument();
  });
});
