/**
 * InlineEdit — click-to-edit text field. Used for renaming threads/projects.
 */

import { useEffect, useRef, useState } from 'react';

interface InlineEditProps {
  value: string;
  onCommit: (next: string) => void | Promise<void>;
  className?: string;
  inputClassName?: string;
  ariaLabel?: string;
  /** External trigger to enter edit mode (e.g. from a parent rename button) */
  editKey?: number;
  /** Render as a single line of N characters max for display */
  maxDisplayChars?: number;
}

export default function InlineEdit({
  value,
  onCommit,
  className = '',
  inputClassName = '',
  ariaLabel,
  editKey,
  maxDisplayChars,
}: InlineEditProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => { setDraft(value); }, [value]);

  useEffect(() => {
    if (editKey !== undefined) {
      setDraft(value);
      setEditing(true);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editKey]);

  useEffect(() => {
    if (editing) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [editing]);

  function commit() {
    setEditing(false);
    const next = draft.trim();
    if (next && next !== value) onCommit(next);
    else setDraft(value);
  }

  function cancel() {
    setEditing(false);
    setDraft(value);
  }

  if (!editing) {
    const display = maxDisplayChars && value.length > maxDisplayChars
      ? `${value.slice(0, maxDisplayChars - 1)}…`
      : value;
    return (
      <span
        className={`inline-edit-display ${className}`}
        onDoubleClick={() => setEditing(true)}
        title={value}
      >
        {display}
      </span>
    );
  }

  return (
    <input
      ref={inputRef}
      className={`inline-edit-input ${inputClassName}`}
      value={draft}
      aria-label={ariaLabel}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === 'Enter') commit();
        else if (e.key === 'Escape') cancel();
      }}
    />
  );
}
