import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import AssemblyPanel from '../components/AssemblyPanel';
import { useAssemblyStore, useViewportStore, useSelectionStore } from '../stores';
import { api } from '../api';

// Mock stores
vi.mock('../stores', () => ({
  useAssemblyStore: vi.fn(),
  useViewportStore: vi.fn(),
  useSelectionStore: vi.fn(),
}));

// Mock API
vi.mock('../api', () => ({
  api: {
    get: vi.fn(),
    downloadFile: vi.fn(),
  },
}));

describe('AssemblyPanel', () => {
  const mockManifest = {
    parts: [
      { name: 'PartA', geometry_stats: { estimated_mass_g: 10.5 } },
      { name: 'PartB', geometry_stats: { estimated_mass_g: 5.2 } },
    ],
    total_parts: 2,
  };

  const mockToggleVisibility = vi.fn();
  const mockSetExplodedFactor = vi.fn();
  const mockSetSelection = vi.fn();
  const mockSetParts = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();

    (useAssemblyStore as any).mockReturnValue({
      partsVisibility: {},
      explodedFactor: 0.5,
      toggleVisibility: mockToggleVisibility,
      setExplodedFactor: mockSetExplodedFactor,
      setParts: mockSetParts,
    });

    (useViewportStore as any).mockReturnValue({
      currentModelId: 'test-model',
      currentProjectId: 'test-project',
    });

    (useSelectionStore as any).mockReturnValue({
      selectedFeatureName: null,
      setSelection: mockSetSelection,
    });

    (api.get as any).mockResolvedValue(mockManifest);
  });

  it('renders correctly and loads manifest', async () => {
    render(<AssemblyPanel />);
    
    // Check header
    expect(screen.getByText('Assembly')).toBeDefined();
    
    // Open panel
    fireEvent.click(screen.getByText('Assembly'));
    
    // Wait for manifest to load
    const partA = await screen.findByText('PartA');
    expect(partA).toBeDefined();
    expect(screen.getByText('PartB')).toBeDefined();
    expect(screen.getByText('10.5 g')).toBeDefined();
  });

  it('toggles part visibility', async () => {
    render(<AssemblyPanel />);
    fireEvent.click(screen.getByText('Assembly'));
    
    const visibilityBtn = await screen.findAllByTitle('Hide part');
    fireEvent.click(visibilityBtn[0]);
    
    expect(mockToggleVisibility).toHaveBeenCalledWith('PartA');
  });

  it('updates exploded factor', async () => {
    render(<AssemblyPanel />);
    fireEvent.click(screen.getByText('Assembly'));
    
    const slider = screen.getByRole('slider');
    fireEvent.change(slider, { target: { value: '1.2' } });
    
    expect(mockSetExplodedFactor).toHaveBeenCalledWith(1.2);
  });

  it('handles part selection', async () => {
    render(<AssemblyPanel />);
    fireEvent.click(screen.getByText('Assembly'));
    
    const partAInfo = await screen.findByText('PartA');
    fireEvent.click(partAInfo);
    
    expect(mockSetSelection).toHaveBeenCalledWith('PartA');
  });

  it('triggers part download', async () => {
    render(<AssemblyPanel />);
    fireEvent.click(screen.getByText('Assembly'));
    
    const stlBtns = await screen.findAllByTitle('Download STL');
    fireEvent.click(stlBtns[0]);
    
    expect(api.downloadFile).toHaveBeenCalledWith(
      '/api/projects/test-project/models/test-model/assembly/PartA/stl',
      'PartA.stl'
    );
  });
});
