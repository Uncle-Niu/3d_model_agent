/**
 * Left sidebar — unified Chats + Model versions navigation.
 */

import { useState } from 'react';
import { useProjectStore, useViewportStore } from '../stores';
import { formatLocalDateTime } from '../time';
import type { ChatThreadSummary, ModelInfo } from '../types';
import InlineEdit from './ui/InlineEdit';

interface HistorySidebarProps {
  versions: ModelInfo[];
  onSelectModel: (modelId: string) => void;
  threads: ChatThreadSummary[];
  activeThreadId: string | null;
  onSelectThread: (threadId: string) => void;
  onNewThread: () => void;
  onRenameThread: (threadId: string, nextTitle: string) => void;
  onDeleteThread: (threadId: string) => void;
}

type Section = 'chats' | 'versions';

function shortId(id: string, n = 6) {
  return id.length <= n ? id : id.slice(-n);
}

export default function HistorySidebar({
  versions,
  onSelectModel,
  threads,
  activeThreadId,
  onSelectThread,
  onNewThread,
  onRenameThread,
  onDeleteThread,
}: HistorySidebarProps) {
  const { currentModelId } = useViewportStore();
  const { project } = useProjectStore();
  const [section, setSection] = useState<Section>('chats');

  if (!project) return null;

  return (
    <aside className="sidebar">
      <div className="sidebar-tabs" role="tablist">
        <button
          className={`sidebar-tab ${section === 'chats' ? 'is-active' : ''}`}
          onClick={() => setSection('chats')}
          role="tab"
          aria-selected={section === 'chats'}
        >
          Chats <span className="sidebar-tab-count">{threads.length}</span>
        </button>
        <button
          className={`sidebar-tab ${section === 'versions' ? 'is-active' : ''}`}
          onClick={() => setSection('versions')}
          role="tab"
          aria-selected={section === 'versions'}
        >
          Versions <span className="sidebar-tab-count">{versions.length}</span>
        </button>
      </div>

      {section === 'chats' && (
        <div className="sidebar-section">
          <div className="sidebar-section-actions">
            <button className="btn btn-primary btn-sm btn-block" onClick={onNewThread}>
              + New chat
            </button>
          </div>
          <div className="sidebar-list">
            {threads.length === 0 ? (
              <div className="sidebar-empty">No chats yet.</div>
            ) : (
              threads.map((t) => (
                <div
                  key={t.thread_id}
                  className={`sidebar-item ${t.thread_id === activeThreadId ? 'is-active' : ''}`}
                  onClick={() => onSelectThread(t.thread_id)}
                >
                  <div className="sidebar-item-main">
                    <InlineEdit
                      value={t.title}
                      onCommit={(next) => onRenameThread(t.thread_id, next)}
                      ariaLabel="Rename chat"
                    />
                    <div className="sidebar-item-meta">
                      {t.message_count} msg{t.message_count === 1 ? '' : 's'}
                      {t.updated_at ? ` · ${formatLocalDateTime(t.updated_at)}` : ''}
                    </div>
                  </div>
                  <button
                    className="sidebar-item-action"
                    title="Delete chat"
                    onClick={(e) => { e.stopPropagation(); onDeleteThread(t.thread_id); }}
                  >
                    ✕
                  </button>
                </div>
              ))
            )}
          </div>
        </div>
      )}

      {section === 'versions' && (
        <div className="sidebar-section">
          <div className="sidebar-list">
            {versions.length === 0 ? (
              <div className="sidebar-empty">No versions generated yet.</div>
            ) : (
              [...versions].reverse().map((v) => {
                const isFinal = v.is_final === true;
                const isFailed = !!v.failure_type;
                const isWip = !isFinal;
                const isActive = v.model_id === currentModelId;
                const labelText = isFailed
                  ? 'Failed iteration'
                  : isFinal
                  ? 'Final'
                  : v.has_glb
                  ? `WIP iter ${v.iteration}`
                  : `WIP iter ${v.iteration} (no geometry)`;
                const labelClass = isFailed
                  ? 'is-failed'
                  : isFinal
                  ? 'is-final'
                  : 'is-wip';
                return (
                  <div
                    key={v.model_id}
                    className={`sidebar-item version-item ${isActive ? 'is-active' : ''} ${isFailed ? 'is-failed' : ''} ${isWip && !isFailed ? 'is-wip' : ''}`}
                    onClick={() => onSelectModel(v.model_id)}
                  >
                    <div className="sidebar-item-main">
                      <div className="version-row">
                        <span className={`version-badge ${labelClass}`} title={labelText}>
                          {labelText}
                        </span>
                        <span className="version-id" title={v.model_id}>#{shortId(v.model_id)}</span>
                        <span className="version-time">{formatLocalDateTime(v.created_at)}</span>
                      </div>
                      <div className="version-prompt" title={v.prompt}>
                        {v.prompt || (v.iteration ? `Iteration ${v.iteration}` : 'Checkpoint')}
                      </div>
                      {v.parent_model_id && (
                        <div className="version-lineage">
                          <span className="version-lineage-label">from</span>
                          <button
                            className="version-lineage-link"
                            title={`Jump to parent ${v.parent_model_id}`}
                            onClick={(e) => { e.stopPropagation(); onSelectModel(v.parent_model_id!); }}
                          >
                            #{shortId(v.parent_model_id)}
                          </button>
                        </div>
                      )}
                      <div className="version-stats">
                        {isFailed && (
                          <span className="version-status is-failed">
                            ⚠ {v.failure_type!.replace(/_/g, ' ')}
                          </span>
                        )}
                        {!isFailed && v.has_glb && (
                          <span className="version-status is-success">✓ Geometry</span>
                        )}
                        {v.vision_score !== undefined && v.vision_score !== null && (
                          <span className="version-score" title="Vision critique score">
                            {Math.round(v.vision_score * 100)}%
                          </span>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>
      )}
    </aside>
  );
}
