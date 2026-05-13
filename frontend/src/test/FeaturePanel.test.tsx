import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import FeaturePanel from '../components/FeaturePanel';
import { api } from '../api';
import { useViewportStore } from '../stores';

vi.mock('../api', () => ({
  api: {
    get: vi.fn()
  }
}));

vi.mock('../stores', () => ({
  useViewportStore: vi.fn()
}));

describe('FeaturePanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (useViewportStore as any).mockReturnValue({
      currentModelId: 'model_1',
      currentProjectId: 'project_1'
    });
  });

  it('renders features when opened', async () => {
    (api.get as any).mockResolvedValue([
      { name: 'box_1', type: 'Workplane', center: [0, 0, 0] },
      { name: 'hole_1', type: 'Workplane', center: [10, 20, 30] }
    ]);

    render(<FeaturePanel />);

    // Initially closed (height 40px but content not visible)
    // The component uses isOpen=false by default
    const header = screen.getByText('Features');
    fireEvent.click(header);

    await waitFor(() => {
      expect(screen.getByText('box_1')).toBeDefined();
      expect(screen.getByText('hole_1')).toBeDefined();
      expect(screen.getByText('(10.0, 20.0, 30.0)')).toBeDefined();
    });
  });
});
