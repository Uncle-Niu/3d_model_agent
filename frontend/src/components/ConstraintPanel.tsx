/**
 * ConstraintPanel — slide-up editor for hard and soft engineering constraints.
 *
 * Hard constraints (deterministic, validated post-generation):
 *   - Print volume (X/Y/Z mm)
 *   - Minimum wall thickness (mm)
 *   - Max file size (MB)
 *
 * Soft constraints (injected into LLM prompt):
 *   - Max overhang angle
 *   - Prefer fillets / chamfers
 *   - Material assumption
 *   - Free-text notes
 */

import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { useProjectStore } from '../stores';
import type { HardConstraints, Project, SoftConstraints } from '../types';

// ---------------------------------------------------------------------------
// Helper — labeled number input
// ---------------------------------------------------------------------------

function NumberField({
  id,
  label,
  unit,
  value,
  min,
  max,
  step,
  onChange,
}: {
  id: string;
  label: string;
  unit?: string;
  value: number;
  min?: number;
  max?: number;
  step?: number;
  onChange: (v: number) => void;
}) {
  return (
    <div className="constraint-field">
      <label htmlFor={id} className="constraint-label">
        {label}
      </label>
      <div className="constraint-input-row">
        <input
          id={id}
          type="number"
          className="constraint-input"
          value={value}
          min={min}
          max={max}
          step={step ?? 1}
          onChange={(e) => onChange(parseFloat(e.target.value) || 0)}
        />
        {unit && <span className="constraint-unit">{unit}</span>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helper — labeled checkbox
// ---------------------------------------------------------------------------

function CheckboxField({
  id,
  label,
  checked,
  onChange,
}: {
  id: string;
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <div className="constraint-field constraint-field--checkbox">
      <input
        id={id}
        type="checkbox"
        className="constraint-checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
      <label htmlFor={id} className="constraint-label">
        {label}
      </label>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main ConstraintPanel
// ---------------------------------------------------------------------------

interface Props {
  isOpen: boolean;
  onClose: () => void;
}

export default function ConstraintPanel({ isOpen, onClose }: Props) {
  const { project, setProject } = useProjectStore();
  const [hard, setHard] = useState<HardConstraints | null>(null);
  const [soft, setSoft] = useState<SoftConstraints | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedMsg, setSavedMsg] = useState('');
  const panelRef = useRef<HTMLDivElement>(null);

  // Initialise local state from project whenever panel opens or project changes
  useEffect(() => {
    if (project) {
      setHard({ ...project.hard_constraints });
      setSoft({ ...project.soft_constraints });
    }
  }, [project, isOpen]);

  // Close on Escape
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape' && isOpen) onClose();
    }
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, onClose]);

  // Close on outside click
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (isOpen && panelRef.current && !panelRef.current.contains(e.target as Node)) {
        onClose();
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [isOpen, onClose]);

  async function handleSave() {
    if (!project || !hard || !soft) return;
    setSaving(true);
    setSavedMsg('');
    try {
      const updated = await api.put<Project>(`/api/projects/${project.project_id}/constraints`, {
        hard_constraints: hard,
        soft_constraints: soft,
      });
      setProject(updated);
      setSavedMsg('✓ Constraints saved');
      setTimeout(() => setSavedMsg(''), 3000);
    } catch (err) {
      setSavedMsg(`❌ Save failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setSaving(false);
    }
  }

  function handleReset() {
    if (!project) return;
    setHard({ ...project.hard_constraints });
    setSoft({ ...project.soft_constraints });
    setSavedMsg('');
  }

  if (!project || !hard || !soft) return null;

  return (
    <>
      {/* Backdrop */}
      <div
        className={`constraint-backdrop ${isOpen ? 'open' : ''}`}
        aria-hidden="true"
      />

      {/* Panel */}
      <div
        ref={panelRef}
        className={`constraint-panel ${isOpen ? 'open' : ''}`}
        role="dialog"
        aria-label="Engineering Constraints Editor"
        aria-modal="true"
      >
        {/* Header */}
        <div className="constraint-header">
          <div className="constraint-header-title">
            <span className="constraint-header-icon">⚙️</span>
            <h2>Engineering Constraints</h2>
          </div>
          <button
            className="constraint-close-btn"
            onClick={onClose}
            aria-label="Close constraints panel"
          >
            ✕
          </button>
        </div>

        <div className="constraint-body">
          {/* ── Hard Constraints ─────────────────────────────────────────────── */}
          <section className="constraint-section">
            <div className="constraint-section-header">
              <span className="constraint-badge constraint-badge--hard">HARD</span>
              <div>
                <h3 className="constraint-section-title">Print Volume</h3>
                <p className="constraint-section-desc">
                  Violations trigger automatic repair. Based on Bambu A1 build volume (256 mm).
                </p>
              </div>
            </div>

            <div className="constraint-grid-3">
              <NumberField
                id="hard-max-x"
                label="Max X"
                unit="mm"
                value={hard.max_x_mm}
                min={10}
                max={1000}
                step={1}
                onChange={(v) => setHard({ ...hard, max_x_mm: v })}
              />
              <NumberField
                id="hard-max-y"
                label="Max Y"
                unit="mm"
                value={hard.max_y_mm}
                min={10}
                max={1000}
                step={1}
                onChange={(v) => setHard({ ...hard, max_y_mm: v })}
              />
              <NumberField
                id="hard-max-z"
                label="Max Z"
                unit="mm"
                value={hard.max_z_mm}
                min={10}
                max={1000}
                step={1}
                onChange={(v) => setHard({ ...hard, max_z_mm: v })}
              />
            </div>

            <div className="constraint-grid-2">
              <NumberField
                id="hard-wall-thickness"
                label="Min Wall Thickness"
                unit="mm"
                value={hard.min_wall_thickness_mm}
                min={0.1}
                max={10}
                step={0.1}
                onChange={(v) => setHard({ ...hard, min_wall_thickness_mm: v })}
              />
              <NumberField
                id="hard-file-size"
                label="Max File Size"
                unit="MB"
                value={hard.max_file_size_mb}
                min={1}
                max={500}
                step={1}
                onChange={(v) => setHard({ ...hard, max_file_size_mb: v })}
              />
            </div>
          </section>

          {/* ── Soft Constraints ─────────────────────────────────────────────── */}
          <section className="constraint-section">
            <div className="constraint-section-header">
              <span className="constraint-badge constraint-badge--soft">SOFT</span>
              <div>
                <h3 className="constraint-section-title">Design Preferences</h3>
                <p className="constraint-section-desc">
                  Injected into the LLM prompt. Checked by vision critique — not hard-enforced.
                </p>
              </div>
            </div>

            <div className="constraint-grid-2">
              <NumberField
                id="soft-overhang"
                label="Max Overhang Angle"
                unit="°"
                value={soft.overhang_angle_max}
                min={0}
                max={90}
                step={5}
                onChange={(v) => setSoft({ ...soft, overhang_angle_max: v })}
              />
              <div className="constraint-field">
                <label htmlFor="soft-material" className="constraint-label">
                  Material
                </label>
                <select
                  id="soft-material"
                  className="constraint-select"
                  value={soft.material}
                  onChange={(e) => setSoft({ ...soft, material: e.target.value })}
                >
                  <option value="PLA">PLA (General purpose)</option>
                  <option value="PETG">PETG (Durable)</option>
                  <option value="ABS">ABS (Heat resistant)</option>
                  <option value="TPU">TPU (Flexible)</option>
                  <option value="ASA">ASA (UV resistant)</option>
                  <option value="Nylon">Nylon (Strong)</option>
                  <option value="Resin">Resin (High detail)</option>
                </select>
              </div>
            </div>

            <div className="constraint-checkboxes">
              <CheckboxField
                id="soft-prefer-fillets"
                label="Prefer fillets (rounded edges)"
                checked={soft.prefer_fillets}
                onChange={(v) => setSoft({ ...soft, prefer_fillets: v })}
              />
              <CheckboxField
                id="soft-prefer-chamfers"
                label="Prefer chamfers (angled edges)"
                checked={soft.prefer_chamfers}
                onChange={(v) => setSoft({ ...soft, prefer_chamfers: v })}
              />
            </div>

            <div className="constraint-field">
              <label htmlFor="soft-notes" className="constraint-label">
                Additional Notes (injected as context)
              </label>
              <textarea
                id="soft-notes"
                className="constraint-textarea"
                value={soft.notes}
                rows={3}
                placeholder="E.g. 'Optimize for speed over detail', 'Use minimal supports', 'Parts must snap together'…"
                onChange={(e) => setSoft({ ...soft, notes: e.target.value })}
              />
            </div>
          </section>
        </div>

        {/* Footer */}
        <div className="constraint-footer">
          {savedMsg && (
            <span
              className={`constraint-save-msg ${savedMsg.startsWith('❌') ? 'error' : 'success'}`}
            >
              {savedMsg}
            </span>
          )}
          <div className="constraint-footer-actions">
            <button
              className="constraint-btn constraint-btn--ghost"
              type="button"
              onClick={handleReset}
              disabled={saving}
            >
              Reset
            </button>
            <button
              className="constraint-btn constraint-btn--primary"
              type="button"
              onClick={handleSave}
              disabled={saving}
            >
              {saving ? 'Saving…' : 'Save Constraints'}
            </button>
          </div>
        </div>
      </div>
    </>
  );
}
