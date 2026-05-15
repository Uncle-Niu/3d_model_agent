/**
 * Imperative themed confirm/prompt dialog. Replaces window.confirm / window.prompt.
 */

import { useEffect, useRef, useState } from 'react';
import { create } from 'zustand';
import Modal from './Modal';

type ConfirmRequest = {
  id: number;
  title: string;
  message: string;
  confirmLabel: string;
  cancelLabel: string;
  tone: 'default' | 'danger';
  resolve: (ok: boolean) => void;
};

type PromptRequest = {
  id: number;
  title: string;
  message?: string;
  initialValue: string;
  placeholder?: string;
  confirmLabel: string;
  resolve: (value: string | null) => void;
};

interface DialogState {
  confirms: ConfirmRequest[];
  prompts: PromptRequest[];
  push: (req: ConfirmRequest | PromptRequest, kind: 'confirm' | 'prompt') => void;
  resolveConfirm: (id: number, ok: boolean) => void;
  resolvePrompt: (id: number, value: string | null) => void;
}

let _nextId = 0;
const useDialogStore = create<DialogState>((set) => ({
  confirms: [],
  prompts: [],
  push: (req, kind) => set((s) => kind === 'confirm'
    ? { confirms: [...s.confirms, req as ConfirmRequest] }
    : { prompts: [...s.prompts, req as PromptRequest] }),
  resolveConfirm: (id, ok) => set((s) => {
    const target = s.confirms.find((r) => r.id === id);
    target?.resolve(ok);
    return { confirms: s.confirms.filter((r) => r.id !== id) };
  }),
  resolvePrompt: (id, value) => set((s) => {
    const target = s.prompts.find((r) => r.id === id);
    target?.resolve(value);
    return { prompts: s.prompts.filter((r) => r.id !== id) };
  }),
}));

export function confirmDialog(opts: {
  title?: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: 'default' | 'danger';
}): Promise<boolean> {
  return new Promise((resolve) => {
    useDialogStore.getState().push({
      id: ++_nextId,
      title: opts.title ?? 'Confirm',
      message: opts.message,
      confirmLabel: opts.confirmLabel ?? 'Confirm',
      cancelLabel: opts.cancelLabel ?? 'Cancel',
      tone: opts.tone ?? 'default',
      resolve,
    }, 'confirm');
  });
}

export function promptDialog(opts: {
  title?: string;
  message?: string;
  initialValue?: string;
  placeholder?: string;
  confirmLabel?: string;
}): Promise<string | null> {
  return new Promise((resolve) => {
    useDialogStore.getState().push({
      id: ++_nextId,
      title: opts.title ?? 'Enter a value',
      message: opts.message,
      initialValue: opts.initialValue ?? '',
      placeholder: opts.placeholder,
      confirmLabel: opts.confirmLabel ?? 'Save',
      resolve,
    }, 'prompt');
  });
}

function PromptBody({ req }: { req: PromptRequest }) {
  const { resolvePrompt } = useDialogStore();
  const [value, setValue] = useState(req.initialValue);
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => { inputRef.current?.focus(); inputRef.current?.select(); }, []);

  return (
    <Modal
      isOpen={true}
      onClose={() => resolvePrompt(req.id, null)}
      title={req.title}
      footer={(
        <>
          <button className="btn btn-ghost" onClick={() => resolvePrompt(req.id, null)}>Cancel</button>
          <button
            className="btn btn-primary"
            onClick={() => resolvePrompt(req.id, value.trim() || null)}
            disabled={!value.trim()}
          >
            {req.confirmLabel}
          </button>
        </>
      )}
    >
      {req.message && <p className="modal-message">{req.message}</p>}
      <input
        ref={inputRef}
        className="modal-input"
        value={value}
        placeholder={req.placeholder}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && value.trim()) resolvePrompt(req.id, value.trim());
        }}
      />
    </Modal>
  );
}

function ConfirmBody({ req }: { req: ConfirmRequest }) {
  const { resolveConfirm } = useDialogStore();
  return (
    <Modal
      isOpen={true}
      onClose={() => resolveConfirm(req.id, false)}
      title={req.title}
      footer={(
        <>
          <button className="btn btn-ghost" onClick={() => resolveConfirm(req.id, false)}>{req.cancelLabel}</button>
          <button
            className={`btn ${req.tone === 'danger' ? 'btn-danger' : 'btn-primary'}`}
            onClick={() => resolveConfirm(req.id, true)}
          >
            {req.confirmLabel}
          </button>
        </>
      )}
    >
      <p className="modal-message">{req.message}</p>
    </Modal>
  );
}

export function DialogHost() {
  const { confirms, prompts } = useDialogStore();
  return (
    <>
      {confirms.map((req) => <ConfirmBody key={req.id} req={req} />)}
      {prompts.map((req) => <PromptBody key={req.id} req={req} />)}
    </>
  );
}
