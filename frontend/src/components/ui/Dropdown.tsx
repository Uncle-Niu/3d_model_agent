/**
 * Dropdown — click-to-toggle menu with outside-click + ESC close.
 */

import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';

interface DropdownProps {
  trigger: (open: boolean) => ReactNode;
  children: (close: () => void) => ReactNode;
  align?: 'left' | 'right';
  className?: string;
  menuClassName?: string;
}

export default function Dropdown({ trigger, children, align = 'left', className = '', menuClassName = '' }: DropdownProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false);
    }
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  return (
    <div ref={rootRef} className={`dropdown ${className}`}>
      <div className="dropdown-trigger" onClick={() => setOpen((v) => !v)}>
        {trigger(open)}
      </div>
      {open && (
        <div className={`dropdown-menu dropdown-menu-${align} ${menuClassName}`}>
          {children(() => setOpen(false))}
        </div>
      )}
    </div>
  );
}
