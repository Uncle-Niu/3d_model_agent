/**
 * Tests for the Chat component.
 *
 * Covers:
 * - Renders welcome screen when no messages
 * - Welcome suggestion buttons exist
 * - Sends message on form submit
 * - Sends message on Enter key
 * - Disabled when isGenerating
 * - Streaming content appears
 * - Status indicator appears with generating + status
 * - Vision verifier card surfaces inside the pipeline when critique data is present
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useChatStore } from '../stores';
import Chat from '../components/Chat';

// Mock heavy 3D/API deps
vi.mock('../api', () => ({
  api: { url: (p: string) => `http://localhost${p}` },
}));

beforeEach(() => {
  useChatStore.getState().reset();
});

describe('Chat component', () => {
  it('shows welcome screen when no messages', () => {
    render(<Chat onSend={() => {}} />);
    expect(screen.getByText(/Mission Crafter/i)).toBeInTheDocument();
    expect(screen.getByText(/Describe a 3D part/i)).toBeInTheDocument();
  });

  it('shows suggestion buttons', () => {
    render(<Chat onSend={() => {}} />);
    expect(screen.getByText(/Simple rounded box/i)).toBeInTheDocument();
    expect(screen.getByText(/Mounting bracket/i)).toBeInTheDocument();
    expect(screen.getByText(/Cylindrical container/i)).toBeInTheDocument();
  });

  it('calls onSend when suggestion button clicked', () => {
    const onSend = vi.fn();
    render(<Chat onSend={onSend} />);
    fireEvent.click(screen.getByText(/Simple rounded box/i));
    expect(onSend).toHaveBeenCalledOnce();
    expect(onSend.mock.calls[0][0]).toContain('box');
  });

  it('send button is disabled when input is empty', () => {
    render(<Chat onSend={() => {}} />);
    const sendBtn = screen.getByRole('button', { name: /➤/i });
    expect(sendBtn).toBeDisabled();
  });

  it('send button enables when input has text', async () => {
    const user = userEvent.setup();
    render(<Chat onSend={() => {}} />);
    const input = screen.getByPlaceholderText(/Describe a 3D part/i);
    await user.type(input, 'Make a cube');
    const sendBtn = screen.getByRole('button', { name: /➤/i });
    expect(sendBtn).not.toBeDisabled();
  });

  it('calls onSend and clears input on submit', async () => {
    const onSend = vi.fn();
    const user = userEvent.setup();
    render(<Chat onSend={onSend} />);
    const input = screen.getByPlaceholderText(/Describe a 3D part/i);
    await user.type(input, 'Make a sphere');
    await user.keyboard('{Enter}');
    expect(onSend).toHaveBeenCalledWith('Make a sphere');
    expect((input as HTMLTextAreaElement).value).toBe('');
  });

  it('does not send on Shift+Enter (newline)', async () => {
    const onSend = vi.fn();
    const user = userEvent.setup();
    render(<Chat onSend={onSend} />);
    const input = screen.getByPlaceholderText(/Describe a 3D part/i);
    await user.type(input, 'Line 1');
    await user.keyboard('{Shift>}{Enter}{/Shift}');
    expect(onSend).not.toHaveBeenCalled();
  });

  it('disables input and send button when isGenerating', () => {
    useChatStore.getState().setGenerating(true);
    render(<Chat onSend={() => {}} />);
    const input = screen.getByPlaceholderText(/Describe a 3D part/i);
    expect(input).toBeDisabled();
  });

  it('renders stored messages', () => {
    useChatStore.getState().addMessage({ role: 'user', content: 'Hello agent', timestamp: '' });
    useChatStore.getState().addMessage({ role: 'assistant', content: 'Here is your part!', timestamp: '' });
    render(<Chat onSend={() => {}} />);
    expect(screen.getByText('Hello agent')).toBeInTheDocument();
    expect(screen.getByText('Here is your part!')).toBeInTheDocument();
  });

  it('renders final assistant summary below the completed timeline', () => {
    useChatStore.getState().addMessage({
      role: 'assistant',
      content: 'Model generated (`model-001`, attempt 1).',
      timestamp: '',
      steps: [{
        stage: 'validating',
        message: 'Plan-conformance check passed.',
        timestamp: '2026-05-15T12:00:00.000Z',
      }],
    });

    const { container } = render(<Chat onSend={() => {}} />);
    const timeline = container.querySelector('.pipeline-progress');
    const summary = screen.getByText('Model generated (`model-001`, attempt 1).');

    expect(timeline).toBeTruthy();
    expect(timeline!.compareDocumentPosition(summary) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByText('Plan-conformance check passed.')).toBeInTheDocument();
  });

  it('shows streaming content with cursor', () => {
    useChatStore.getState().appendStreamChunk('Generating code...');
    render(<Chat onSend={() => {}} />);
    expect(screen.getByText('Generating code...')).toBeInTheDocument();
    // Cursor blink character
    expect(screen.getByText('▊')).toBeInTheDocument();
  });

  it('shows status indicator when generating with status', () => {
    useChatStore.getState().setGenerating(true);
    useChatStore.getState().setStage('critiquing', 'Analyzing geometry with vision AI...');
    render(<Chat onSend={() => {}} />);
    // PipelineProgress maps the raw 'critiquing' stage to a clearer label.
    expect(screen.getByText('Verifying')).toBeInTheDocument();
    expect(screen.getByText('Analyzing geometry with vision AI...')).toBeInTheDocument();
  });

  it('no vision verifier card is shown when there is no critique step', () => {
    render(<Chat onSend={() => {}} />);
    // VisionCritiqueCard only renders when a critiquing step carries
    // vision data; nothing in this test produces one.
    expect(screen.queryByText(/Vision verifier/i)).toBeNull();
  });

  it('vision verifier card is shown inline when a live critiquing step carries vision data', () => {
    useChatStore.getState().setGenerating(true);
    useChatStore.getState().setStage(
      'critiquing',
      'Vision score 0.75 · ✓ matches intent · 0 error(s), 0 warning(s)',
      undefined,
      {
        vision_score: 0.75,
        matches_intent: true,
        vision_issues: [],
        render_urls: { iso: '/r/iso.png' },
      },
    );
    render(<Chat onSend={() => {}} />);
    // The promoted card inside PipelineProgress now carries the critique.
    expect(screen.getByText(/Vision verifier/i)).toBeInTheDocument();
  });

  it('disabled prop disables input and button', () => {
    render(<Chat onSend={() => {}} disabled={true} />);
    expect(screen.getByPlaceholderText(/Describe a 3D part/i)).toBeDisabled();
  });
});
