import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import ParameterPanel from '../components/ParameterPanel';
import { api } from '../api';
import { useViewportStore } from '../stores';

// Mock dependencies
vi.mock('../api', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    url: vi.fn((path) => `http://localhost:8000${path}`)
  }
}));

vi.mock('../stores', () => ({
  useViewportStore: vi.fn()
}));

describe('ParameterPanel', () => {
  const mockSetModel = vi.fn();
  
  beforeEach(() => {
    vi.clearAllMocks();
    (useViewportStore as any).mockReturnValue({
      currentModelId: 'model_1',
      currentProjectId: 'project_1',
      setModel: mockSetModel
    });
  });

  it('renders loading state then parameters', async () => {
    (api.get as any).mockResolvedValue([
      { name: 'length', value: 100, type: 'int' },
      { name: 'is_active', value: true, type: 'bool' }
    ]);

    render(<ParameterPanel />);

    expect(screen.getByText('Parameters')).toBeDefined();
    
    await waitFor(() => {
      expect(screen.getByLabelText('length')).toBeDefined();
      expect(screen.getByLabelText('is_active')).toBeDefined();
    });

    const lengthInput = screen.getByLabelText('length') as HTMLInputElement;
    expect(lengthInput.value).toBe('100');
    
    const activeSelect = screen.getByLabelText('is_active') as HTMLSelectElement;
    expect(activeSelect.value).toBe('true');
  });

  it('handles value changes and updates model', async () => {
    (api.get as any).mockResolvedValue([
      { name: 'length', value: 100, type: 'int' }
    ]);
    
    (api.post as any).mockResolvedValue({
      success: true,
      message: 'Updated',
      model: { model_id: 'model_2' },
      glb_url: '/glb/2'
    });

    render(<ParameterPanel />);

    await waitFor(() => expect(screen.getByLabelText('length')).toBeDefined());

    const lengthInput = screen.getByLabelText('length') as HTMLInputElement;
    fireEvent.change(lengthInput, { target: { value: '200' } });

    const updateBtn = screen.getByText('Update Model');
    expect(updateBtn.hasAttribute('disabled')).toBe(false);

    fireEvent.click(updateBtn);

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        '/api/projects/project_1/models/model_1/update_parameters',
        { parameters: { length: 200 } }
      );
      expect(mockSetModel).toHaveBeenCalledWith('model_2', 'http://localhost:8000/glb/2', 'project_1');
    });
  });
});
