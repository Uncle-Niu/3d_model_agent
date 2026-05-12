/**
 * DebugPanel — collapsible panel showing raw LLM request/response
 * and all pipeline debug_log messages in real time.
 */

import { useEffect, useRef, useState } from 'react';
import { useDebugStore } from '../stores';
import type { DebugEntry } from '../types';

// Category → badge color
const CATEGORY_COLORS: Record<string, string> = {
  ollama: '#4caf7a',
  llm_request: '#e0a040',
  llm_response: '#4a90d9',
  llm_error: '#d94a5a',
  code_extraction: '#9b59b6',
  cadquery_exec: '#3498db',
  cadquery_result: '#1abc9c',
  repair_request: '#e67e22',
  repair_response: '#f39c12',
  model_ready: '#2ecc71',
  pipeline_error: '#e74c3c',
  init: '#7f8c8d',
  ws: '#95a5a6',
  error: '#d94a5a',
};

function getCategoryColor(category: string): string {
  return CATEGORY_COLORS[category] || '#606080';
}

function EntryRow({ entry, isExpanded, onToggle }: {
  entry: DebugEntry;
  isExpanded: boolean;
  onToggle: () => void;
}) {
  const ts = entry.timestamp.replace('T', ' ').replace('Z', '').slice(11, 23);

  return (
    <div className="debug-entry">
      <div className="debug-entry-header" onClick={onToggle}>
        <span className="debug-ts">{ts}</span>
        <span
          className="debug-badge"
          style={{ background: getCategoryColor(entry.category) }}
        >
          {entry.category}
        </span>
        <span className="debug-msg">{entry.message}</span>
        {entry.data && (
          <span className="debug-expand-icon">{isExpanded ? '▾' : '▸'}</span>
        )}
      </div>
      {isExpanded && entry.data && (
        <div className="debug-entry-data">
          <pre>{JSON.stringify(entry.data, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}

export default function DebugPanel() {
  const { entries, isOpen, toggleOpen, clear } = useDebugStore();
  const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set());
  const scrollRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom when new entries arrive
  useEffect(() => {
    if (isOpen && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [entries, isOpen]);

  const toggleEntry = (id: number) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  const expandAll = () => {
    setExpandedIds(new Set(entries.map(e => e.id)));
  };

  const collapseAll = () => {
    setExpandedIds(new Set());
  };

  return (
    <div className={`debug-panel ${isOpen ? 'debug-panel-open' : ''}`}>
      {/* Toggle bar */}
      <div className="debug-toggle-bar" onClick={toggleOpen}>
        <span className="debug-toggle-icon">{isOpen ? '▾' : '▸'}</span>
        <span className="debug-toggle-label">
          Debug Log
        </span>
        <span className="debug-entry-count">{entries.length}</span>
        {isOpen && (
          <div className="debug-toolbar" onClick={(e) => e.stopPropagation()}>
            <button onClick={expandAll} title="Expand all">⊞</button>
            <button onClick={collapseAll} title="Collapse all">⊟</button>
            <button onClick={clear} title="Clear log">✕</button>
          </div>
        )}
      </div>

      {/* Log entries */}
      {isOpen && (
        <div className="debug-entries" ref={scrollRef}>
          {entries.length === 0 ? (
            <div className="debug-empty">
              No debug messages yet. Send a chat message to see raw LLM request/response data.
            </div>
          ) : (
            entries.map((entry) => (
              <EntryRow
                key={entry.id}
                entry={entry}
                isExpanded={expandedIds.has(entry.id)}
                onToggle={() => toggleEntry(entry.id)}
              />
            ))
          )}
        </div>
      )}
    </div>
  );
}
