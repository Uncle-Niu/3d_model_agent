/**
 * Component tests for FeaturePanel.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { useViewportStore, useSelectionStore } from '../stores';
import FeaturePanel from '../components/FeaturePanel';
import { api } from '../api';

// Mock the api module
vi.mock('../api', () => ({
  api: {
    get: vi.fn(),
  },
}));

describe('FeaturePanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    act(() => {
      useViewportStore.getState().reset();
      useSelectionStore.getState().clearSelection();
    });
  });

  it('renders nothing when no model is selected', () => {
    const { container } = render(<FeaturePanel />);
    expect(container.firstChild).toBeNull();
  });

  it('renders "Features" header when model is selected', async () => {
    (api.get as any).mockResolvedValue([]);
    act(() => {
      useViewportStore.getState().setModel('model-1', 'url', 'proj-1');
    });
    render(<FeaturePanel />);
    expect(screen.getByText('Features')).toBeInTheDocument();
  });

  it('loads and displays features when opened', async () => {
    const mockFeatures = [
      { name: 'base', type: 'workplane', center: [0, 0, 0] },
      { name: 'hole', type: 'cut', center: [10, 20, 30] },
    ];
    (api.get as any).mockResolvedValue(mockFeatures);

    act(() => {
      useViewportStore.getState().setModel('model-1', 'url', 'proj-1');
    });
    render(<FeaturePanel />);
    
    // Click header to open
    fireEvent.click(screen.getByText('Features'));
    
    await waitFor(() => {
      expect(screen.getByText('base')).toBeInTheDocument();
      expect(screen.getByText('hole')).toBeInTheDocument();
    });

    expect(screen.getByText('workplane')).toBeInTheDocument();
    expect(screen.getByText('(10.0, 20.0, 30.0)')).toBeInTheDocument();
  });

  it('shows empty message when no features returned', async () => {
    (api.get as any).mockResolvedValue([]);
    act(() => {
      useViewportStore.getState().setModel('model-1', 'url', 'proj-1');
    });
    render(<FeaturePanel />);
    
    fireEvent.click(screen.getByText('Features'));
    
    await waitFor(() => {
      expect(screen.getByText('No features detected.')).toBeInTheDocument();
    });
  });

  it('highlights feature on click and updates selection store', async () => {
    const mockFeatures = [
      { name: 'base', type: 'workplane', center: [0, 0, 0] },
    ];
    (api.get as any).mockResolvedValue(mockFeatures);

    act(() => {
      useViewportStore.getState().setModel('model-1', 'url', 'proj-1');
    });
    render(<FeaturePanel />);
    
    fireEvent.click(screen.getByText('Features'));
    
    await waitFor(() => screen.getByText('base'));
    
    const item = screen.getByText('base').closest('li');
    fireEvent.click(item!);
    
    expect(useSelectionStore.getState().selectedFeatureName).toBe('base');
    expect(item).toHaveClass('active');

    // Click again to toggle off
    fireEvent.click(item!);
    expect(useSelectionStore.getState().selectedFeatureName).toBeNull();
    expect(item).not.toHaveClass('active');
  });

  it('reflects external selection from store', async () => {
    const mockFeatures = [
      { name: 'base', type: 'workplane', center: [0, 0, 0] },
    ];
    (api.get as any).mockResolvedValue(mockFeatures);

    act(() => {
      useViewportStore.getState().setModel('model-1', 'url', 'proj-1');
    });
    render(<FeaturePanel />);
    
    fireEvent.click(screen.getByText('Features'));
    
    await waitFor(() => screen.getByText('base'));
    
    act(() => {
      useSelectionStore.getState().setSelection('base');
    });
    
    await waitFor(() => {
        expect(screen.getByText('base').closest('li')).toHaveClass('active');
    });
  });
});
