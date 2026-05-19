/**
 * Chat interface — message list, input, suggestion chips, and critique inline.
 */

import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { useChatStore, useSelectionStore } from '../stores';
import { formatLocalDateTime } from '../time';
import type { AgentLogic } from '../types';
import PipelineProgress from './PipelineProgress';
import AppIcon from './AppIcon';

interface ChatProps {
  onSend: (message: string, agentLogic: AgentLogic) => void;
  onCancel?: () => void;
  disabled?: boolean;
}

const SUGGESTIONS: Array<{ label: string; prompt: string }> = [
  { label: 'Simple rounded box', prompt: 'Create a simple box with rounded edges, 50x30x10mm' },
  { label: 'Mounting bracket', prompt: 'Make a mounting bracket with two screw holes' },
  { label: 'Cylindrical container', prompt: 'Design a cylindrical container with a flat bottom, 25mm radius, 40mm tall' },
  { label: 'Snap-fit cable clip', prompt: 'Create a cable clip that can snap onto a 4mm wire, with a hinge opening' },
];

export default function Chat({ onSend, onCancel, disabled = false }: ChatProps) {
  const [input, setInput] = useState('');
  const [agentLogic, setAgentLogic] = useState<AgentLogic>('orchestrator');
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const { selectedFeatureName } = useSelectionStore();

  const {
    messages,
    streamingContent,
    isGenerating,
    currentSteps,
  } = useChatStore();

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, streamingContent, currentSteps.length, isGenerating]);

  // Auto-grow textarea
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [input]);

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const trimmed = input.trim();
    if (!trimmed || isGenerating || disabled) return;
    onSend(trimmed, agentLogic);
    setInput('');
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  }

  function insertSelectedReference() {
    if (!selectedFeatureName) return;
    const ref = `@${selectedFeatureName} `;
    const el = inputRef.current;
    if (!el) {
      setInput((v) => v + ref);
      return;
    }
    const start = el.selectionStart ?? input.length;
    const end = el.selectionEnd ?? input.length;
    const next = input.slice(0, start) + ref + input.slice(end);
    setInput(next);
    requestAnimationFrame(() => {
      el.focus();
      const caret = start + ref.length;
      el.setSelectionRange(caret, caret);
    });
  }

  const isEmpty = messages.length === 0 && !isGenerating;

  return (
    <div className="chat-container">
      <div className="chat-messages">
        {isEmpty && (
          <div className="chat-welcome">
            <div className="chat-welcome-icon" aria-hidden="true">
              <AppIcon size={56} />
            </div>
            <h3>Mission Crafter</h3>
            <p>Describe a 3D part and I'll generate it for you.</p>
            <div className="chat-suggestions">
              {SUGGESTIONS.map((s) => (
                <button key={s.label} onClick={() => onSend(s.prompt, agentLogic)}>{s.label}</button>
              ))}
            </div>
          </div>
        )}

        {messages.map((msg, i) => {
          const hasSteps = !!(msg.steps && msg.steps.length > 0);
          const hasContent = msg.content.trim().length > 0;
          return (
            <div key={i} className={`chat-message chat-message-${msg.role}`}>
              <div className="chat-message-avatar" aria-hidden="true">
                {msg.role === 'user' ? '◔' : <AppIcon size={16} />}
              </div>
              <div className="chat-message-content">
                <div className="chat-message-meta">
                  <span>{msg.role === 'user' ? 'You' : 'Mission Crafter'}</span>
                  <time dateTime={msg.timestamp}>{formatLocalDateTime(msg.timestamp)}</time>
                </div>
                {!hasSteps && hasContent && <div className="chat-message-text">{msg.content}</div>}
                {hasSteps && (
                  <PipelineProgress steps={msg.steps!} defaultShowTimeline={true} />
                )}
                {hasSteps && hasContent && <div className="chat-message-text">{msg.content}</div>}
              </div>
            </div>
          );
        })}

        {streamingContent && (
          <div className="chat-message chat-message-assistant">
            <div className="chat-message-avatar" aria-hidden="true"><AppIcon size={16} /></div>
            <div className="chat-message-content streaming">
              <div className="chat-message-text">{streamingContent}</div>
              <span className="cursor-blink">▊</span>
            </div>
          </div>
        )}

        {isGenerating && (
          <div className="chat-message chat-message-assistant">
            <div className="chat-message-avatar" aria-hidden="true"><AppIcon size={16} /></div>
            <div className="chat-message-content">
              {/* PipelineProgress hoists the vision verifier card (with
                  render thumbnails + score + issue list) to the top of
                  its timeline, so the user sees what the agent saw the
                  moment the verifier returns. */}
              <PipelineProgress steps={currentSteps} isLive={true} />
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      <form className="chat-input-form" onSubmit={handleSubmit}>
        {selectedFeatureName && (
          <div className="chat-input-tools">
            <button
              type="button"
              className="chat-tool-btn chat-tool-selected"
              onClick={insertSelectedReference}
              title={`Insert reference to selected part: ${selectedFeatureName}`}
              disabled={disabled}
            >
              @{selectedFeatureName.length > 18 ? `${selectedFeatureName.slice(0, 17)}…` : selectedFeatureName}
            </button>
          </div>
        )}
        <div className="chat-input-tools">
          <div className="chat-logic-toggle" role="group" aria-label="Agent logic for this chat turn">
            <button
              type="button"
              className={agentLogic === 'orchestrator' ? 'active' : ''}
              onClick={() => setAgentLogic('orchestrator')}
              disabled={isGenerating || disabled}
              title="Use the deterministic backend orchestrator"
            >
              Orchestrator
            </button>
            <button
              type="button"
              className={agentLogic === 'llm_agent' ? 'active' : ''}
              onClick={() => setAgentLogic('llm_agent')}
              disabled={isGenerating || disabled}
              title="Ask the LLM agent to choose this turn's workflow policy"
            >
              LLM agent
            </button>
          </div>
        </div>
        <div className="chat-input-row">
          <textarea
            ref={inputRef}
            className="chat-input"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={selectedFeatureName
              ? `Describe a change (use @ to reference ${selectedFeatureName})…`
              : 'Describe a 3D part to generate…'}
            rows={1}
            disabled={isGenerating || disabled}
          />
          {isGenerating ? (
            <button
              className="chat-send-btn btn btn-stop"
              type="button"
              onClick={() => onCancel?.()}
              disabled={!onCancel}
              title="Stop the in-progress chat turn"
            >
              ■
            </button>
          ) : (
            <button
              className="chat-send-btn btn btn-primary"
              type="submit"
              disabled={!input.trim() || disabled}
              title="Send (Enter)"
            >
              ➤
            </button>
          )}
        </div>
      </form>
    </div>
  );
}
