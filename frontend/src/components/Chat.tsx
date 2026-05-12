/**
 * Chat interface component — message list + input.
 */

import { FormEvent, useEffect, useRef, useState } from 'react';
import { useChatStore } from '../stores';

interface ChatProps {
  onSend: (message: string) => void;
}

export default function Chat({ onSend }: ChatProps) {
  const [input, setInput] = useState('');
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const {
    messages,
    streamingContent,
    isGenerating,
    currentStage,
    currentStatus,
  } = useChatStore();

  // Auto-scroll to bottom
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, streamingContent, currentStatus]);

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    const trimmed = input.trim();
    if (!trimmed || isGenerating) return;
    onSend(trimmed);
    setInput('');
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
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
            </div>
          </div>
        )}

        {messages.map((msg, i) => (
          <div key={i} className={`chat-message chat-message-${msg.role}`}>
            <div className="chat-message-avatar">
              {msg.role === 'user' ? '👤' : '🤖'}
            </div>
            <div className="chat-message-content">
              <pre>{msg.content}</pre>
            </div>
          </div>
        ))}

        {/* Streaming response */}
        {streamingContent && (
          <div className="chat-message chat-message-assistant">
            <div className="chat-message-avatar">🤖</div>
            <div className="chat-message-content streaming">
              <pre>{streamingContent}</pre>
              <span className="cursor-blink">▊</span>
            </div>
          </div>
        )}

        {/* Status indicator */}
        {isGenerating && currentStatus && (
          <div className="chat-status">
            <div className="chat-status-dot" />
            <span className="chat-status-stage">{currentStage}</span>
            <span>{currentStatus}</span>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

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
          disabled={isGenerating}
        />
        <button
          className="chat-send-btn"
          type="submit"
          disabled={!input.trim() || isGenerating}
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
