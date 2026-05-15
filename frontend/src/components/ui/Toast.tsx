/**
 * Toast notifications — themed, stacked bottom-right.
 */

import { useEffect } from 'react';
import { create } from 'zustand';

type ToastKind = 'info' | 'success' | 'error' | 'warning';

interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
  duration: number;
}

interface ToastState {
  items: ToastItem[];
  push: (kind: ToastKind, message: string, duration?: number) => void;
  dismiss: (id: number) => void;
}

let _id = 0;
export const useToastStore = create<ToastState>((set) => ({
  items: [],
  push: (kind, message, duration = 4000) => {
    const id = ++_id;
    set((s) => ({ items: [...s.items, { id, kind, message, duration }] }));
  },
  dismiss: (id) => set((s) => ({ items: s.items.filter((t) => t.id !== id) })),
}));

export const toast = {
  info: (m: string, d?: number) => useToastStore.getState().push('info', m, d),
  success: (m: string, d?: number) => useToastStore.getState().push('success', m, d),
  error: (m: string, d?: number) => useToastStore.getState().push('error', m, d ?? 6000),
  warning: (m: string, d?: number) => useToastStore.getState().push('warning', m, d),
};

function ToastRow({ item }: { item: ToastItem }) {
  const dismiss = useToastStore((s) => s.dismiss);
  useEffect(() => {
    const t = window.setTimeout(() => dismiss(item.id), item.duration);
    return () => window.clearTimeout(t);
  }, [item.id, item.duration, dismiss]);

  const icon = item.kind === 'success' ? '✓'
    : item.kind === 'error' ? '✕'
    : item.kind === 'warning' ? '⚠'
    : 'ⓘ';

  return (
    <div className={`toast toast-${item.kind}`} role="status">
      <span className="toast-icon">{icon}</span>
      <span className="toast-message">{item.message}</span>
      <button className="toast-dismiss" onClick={() => dismiss(item.id)} aria-label="Dismiss">✕</button>
    </div>
  );
}

export function ToastHost() {
  const items = useToastStore((s) => s.items);
  return (
    <div className="toast-host">
      {items.map((item) => <ToastRow key={item.id} item={item} />)}
    </div>
  );
}
