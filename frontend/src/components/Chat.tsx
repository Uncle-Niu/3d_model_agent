/**
 * Chat interface component — message list + input + critique panel.
 */

import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { useChatStore } from '../stores';
import { formatLocalDateTime } from '../time';
import CritiquePanel from './CritiquePanel';
import PipelineProgress from './PipelineProgress';

interface ChatProps {
  onSend: (message: string) => void;
  disabled?: boolean;
}

export default function Chat({ onSend, disabled = false }: ChatProps) {
  const [input, setInput] = useState('');
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const {
    messages,
    streamingContent,
    isGenerating,
    currentStage,
    currentStatus,
    currentSteps,
  } = useChatStore();

  // Auto-scroll to bottom
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, streamingContent, currentStatus]);

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    const trimmed = input.trim();
    if (!trimmed || isGenerating || disabled) return;
    onSend(trimmed);
    setInput('');
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  // Stage → icon/label
  const STAGE_ICONS: Record<string, string> = {
    generating:  '✍️',
    executing:   '⚙️',
    tessellating:'🔺',
    rendering:   '📷',
    critiquing:  '👁',
    repairing:   '🔧',
    failed:      '❌',
    validating:  '✅',
  };

  return (
    <div className="chat-container">
      {/* Messages */}
      <div className="chat-messages">
        {messages.length === 0 && !isGenerating && (
          <div className="chat-welcome">
            <div className="chat-welcome-icon">⚙️</div>
            <h3>AI CAD Agent</h3>
            <p>Describe a 3D part and I'll generate it for you.</p>
            <div className="chat-suggestions">
              <button onClick={() => onSend('Create a simple box with rounded edges, 50x30x10mm')}>
                📦 Simple rounded box
              </button>
              <button onClick={() => onSend('Make a mounting bracket with two screw holes')}>
                🔩 Mounting bracket
              </button>
              <button onClick={() => onSend('Design a cylindrical container with a flat bottom, 25mm radius, 40mm tall')}>
                🥫 Cylindrical container
              </button>
              <button onClick={() => onSend('Create a cable clip that can snap onto a 4mm wire, with a hinge opening')}>
                📎 Snap-fit cable clip
              </button>
            </div>
          </div>
        )}

        {messages.map((msg, i) => (
          <div key={i} className={`chat-message chat-message-${msg.role}`}>
            <div className="chat-message-avatar">
              {msg.role === 'user' ? '👤' : '🤖'}
            </div>
            <div className="chat-message-content">
              <div className="chat-message-meta">
                <span>{msg.role === 'user' ? 'You' : 'CAD Agent'}</span>
                <time dateTime={msg.timestamp}>{formatLocalDateTime(msg.timestamp)}</time>
              </div>
              <pre className="chat-message-text">{msg.content}</pre>
              {msg.steps && msg.steps.length > 0 && (
                <PipelineProgress steps={msg.steps} />
              )}
            </div>
          </div>
        ))}

        {/* Streaming response */}
        {streamingContent && (
          <div className="chat-message chat-message-assistant">
            <div className="chat-message-avatar">🤖</div>
            <div className="chat-message-content streaming">
              <pre className="chat-message-text">{streamingContent}</pre>
              <span className="cursor-blink">▊</span>
            </div>
          </div>
        )}

        {/* Live Progress */}
        {isGenerating && (
          <div className="chat-message chat-message-assistant">
            <div className="chat-message-avatar">🤖</div>
            <div className="chat-message-content">
              <PipelineProgress steps={currentSteps} isLive={true} />
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Vision critique panel — shown below messages, above input */}
      <CritiquePanel />

      {/* Input */}
      <form className="chat-input-form" onSubmit={handleSubmit}>
        <textarea
          ref={inputRef}
          className="chat-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Describe a 3D part to generate..."
          rows={1}
          disabled={isGenerating || disabled}
        />
        <button
          className="chat-send-btn"
          type="submit"
          disabled={!input.trim() || isGenerating || disabled}
        >
          {isGenerating ? (
            <span className="spinner" />
          ) : (
            '➤'
          )}
        </button>
      </form>
    </div>
  );
}
