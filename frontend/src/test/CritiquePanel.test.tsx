/**
 * Component tests for CritiquePanel.
 *
 * Tests that the panel:
 * - Renders nothing when no critique
 * - Shows score, issues, and dismiss button when critique is set
 * - Calls clearCritique when dismiss is clicked
 * - Shows "no issues" when issue list is empty
 * - Shows intent warning when matchesIntent = false
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { useCritiqueStore } from '../stores';
import CritiquePanel from '../components/CritiquePanel';

// Mock the api module — we don't need real URLs in tests
vi.mock('../api', () => ({
  api: {
    url: (path: string) => `http://localhost${path}`,
  },
}));

beforeEach(() => {
  useCritiqueStore.getState().clearCritique();
});

describe('CritiquePanel', () => {
  it('renders nothing when no critique', () => {
    const { container } = render(<CritiquePanel />);
    expect(container.firstChild).toBeNull();
  });

  it('renders panel when critique is set', () => {
    useCritiqueStore.getState().setCritique({
      score: 0.82,
      matchesIntent: true,
      issues: [],
      renderUrls: {},
    });
    render(<CritiquePanel />);
    expect(screen.getByText(/Vision Critique/i)).toBeInTheDocument();
  });

  it('shows score percentage', () => {
    useCritiqueStore.getState().setCritique({
      score: 0.82,
      matchesIntent: true,
      issues: [],
      renderUrls: {},
    });
    render(<CritiquePanel />);
    expect(screen.getByText('82%')).toBeInTheDocument();
  });

  it('shows "Good" label for score >= 0.8', () => {
    useCritiqueStore.getState().setCritique({
      score: 0.90, matchesIntent: true, issues: [], renderUrls: {},
    });
    render(<CritiquePanel />);
    expect(screen.getByText('Good')).toBeInTheDocument();
  });

  it('shows "Needs Improvement" label for score 0.65-0.8', () => {
    useCritiqueStore.getState().setCritique({
      score: 0.70, matchesIntent: true, issues: [], renderUrls: {},
    });
    render(<CritiquePanel />);
    expect(screen.getByText('Needs Improvement')).toBeInTheDocument();
  });

  it('shows "Poor" label for score < 0.65', () => {
    useCritiqueStore.getState().setCritique({
      score: 0.40, matchesIntent: false, issues: [], renderUrls: {},
    });
    render(<CritiquePanel />);
    expect(screen.getByText(/Poor/i)).toBeInTheDocument();
  });

  it('shows no-issues message when issues list is empty', () => {
    useCritiqueStore.getState().setCritique({
      score: 0.95, matchesIntent: true, issues: [], renderUrls: {},
    });
    render(<CritiquePanel />);
    expect(screen.getByText(/No printability issues/i)).toBeInTheDocument();
  });

  it('renders issues with severity badges', () => {
    useCritiqueStore.getState().setCritique({
      score: 0.5,
      matchesIntent: true,
      issues: [
        { issue_type: 'thin_wall', severity: 'error', description: 'Wall is too thin', location_hint: 'side' },
        { issue_type: 'overhang', severity: 'warning', description: 'Steep overhang detected', location_hint: '' },
      ],
      renderUrls: {},
    });
    render(<CritiquePanel />);
    expect(screen.getByText('Wall is too thin')).toBeInTheDocument();
    expect(screen.getByText('Steep overhang detected')).toBeInTheDocument();
    expect(screen.getByText(/✕ Error/i)).toBeInTheDocument();
    expect(screen.getByText(/⚠ Warning/i)).toBeInTheDocument();
  });

  it('shows location hint when provided', () => {
    useCritiqueStore.getState().setCritique({
      score: 0.5,
      matchesIntent: true,
      issues: [
        { issue_type: 'thin_wall', severity: 'error', description: 'Too thin', location_hint: 'bottom face' },
      ],
      renderUrls: {},
    });
    render(<CritiquePanel />);
    expect(screen.getByText(/@ bottom face/i)).toBeInTheDocument();
  });

  it('shows intent warning when matchesIntent = false', () => {
    useCritiqueStore.getState().setCritique({
      score: 0.6,
      matchesIntent: false,
      issues: [],
      renderUrls: {},
    });
    render(<CritiquePanel />);
    expect(screen.getByText(/may not fully match/i)).toBeInTheDocument();
  });

  it('does not show intent warning when matchesIntent = true', () => {
    useCritiqueStore.getState().setCritique({
      score: 0.9, matchesIntent: true, issues: [], renderUrls: {},
    });
    render(<CritiquePanel />);
    expect(screen.queryByText(/may not fully match/i)).toBeNull();
  });

  it('dismiss button calls clearCritique', () => {
    useCritiqueStore.getState().setCritique({
      score: 0.8, matchesIntent: true, issues: [], renderUrls: {},
    });
    render(<CritiquePanel />);
    fireEvent.click(screen.getByTitle('Dismiss'));
    expect(useCritiqueStore.getState().critique).toBeNull();
  });

  it('renders render thumbnails when renderUrls provided', () => {
    useCritiqueStore.getState().setCritique({
      score: 0.9,
      matchesIntent: true,
      issues: [],
      renderUrls: {
        iso: '/api/renders/iso',
        front: '/api/renders/front',
      },
    });
    render(<CritiquePanel />);
    const imgs = screen.getAllByRole('img');
    expect(imgs.length).toBeGreaterThanOrEqual(2);
  });

  it('render labels show view names', () => {
    useCritiqueStore.getState().setCritique({
      score: 0.9,
      matchesIntent: true,
      issues: [],
      renderUrls: { iso: '/api/renders/iso', top: '/api/renders/top' },
    });
    render(<CritiquePanel />);
    expect(screen.getByText('ISO')).toBeInTheDocument();
    expect(screen.getByText('TOP')).toBeInTheDocument();
  });
});
