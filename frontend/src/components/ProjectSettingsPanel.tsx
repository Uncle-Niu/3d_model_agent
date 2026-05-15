/**
 * ProjectSettingsPanel — slide-up editor for project settings, constraints, and global defaults.
 */

import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { useProjectStore } from '../stores';
import type { HardConstraints, Project, SoftConstraints, GlobalSettings } from '../types';
import { toast } from './ui/Toast';

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
  disabled
}: {
  id: string;
  label: string;
  unit?: string;
  value: number;
  min?: number;
  max?: number;
  step?: number;
  onChange: (v: number) => void;
  disabled?: boolean;
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
          disabled={disabled}
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
  disabled
}: {
  id: string;
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <div className="constraint-field constraint-field--checkbox">
      <input
        id={id}
        type="checkbox"
        className="constraint-checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        disabled={disabled}
      />
      <label htmlFor={id} className="constraint-label">
        {label}
      </label>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main ProjectSettingsPanel
// ---------------------------------------------------------------------------

interface Props {
  isOpen: boolean;
  onClose: () => void;
  onRenameProject: (newName: string) => void;
  onDeleteProject: () => void;
  onOpenProjectFolder: () => void;
}

type EditMode = 'project' | 'global';

export default function ProjectSettingsPanel({ isOpen, onClose, onRenameProject, onDeleteProject, onOpenProjectFolder }: Props) {
  const { project, setProject } = useProjectStore();
  const [hard, setHard] = useState<HardConstraints | null>(null);
  const [soft, setSoft] = useState<SoftConstraints | null>(null);
  const [projectName, setProjectName] = useState('');
  
  const [mode, setMode] = useState<EditMode>('project');
  
  const [saving, setSaving] = useState(false);
  const [savedMsg, setSavedMsg] = useState('');
  const panelRef = useRef<HTMLDivElement>(null);

  // Load state based on mode
  useEffect(() => {
    if (!isOpen) return;
    
    if (mode === 'project' && project) {
      setHard({ ...project.hard_constraints });
      setSoft({ ...project.soft_constraints });
      setProjectName(project.name);
    } else if (mode === 'global') {
      fetchGlobalSettings();
    }
  }, [project, isOpen, mode]);

  async function fetchGlobalSettings() {
    try {
      const settings = await api.get<GlobalSettings>('/api/settings/defaults');
      setHard({ ...settings.hard_constraints });
      setSoft({ ...settings.soft_constraints });
    } catch (err) {
      console.error("Failed to load global settings", err);
    }
  }

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
    if (!hard || !soft) return;
    setSaving(true);
    setSavedMsg('');
    try {
      if (mode === 'project') {
        if (!project) return;
        if (projectName.trim() && projectName !== project.name) {
          onRenameProject(projectName);
        }
        const updated = await api.put<Project>(`/api/projects/${project.project_id}/constraints`, {
          hard_constraints: hard,
          soft_constraints: soft,
        });
        setProject(updated);
        toast.success('Project constraints saved');
      } else {
        await api.put<GlobalSettings>('/api/settings/defaults', {
          hard_constraints: hard,
          soft_constraints: soft,
        });
        toast.success('Global defaults saved');
      }
    } catch (err) {
      toast.error(`Save failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setSaving(false);
    }
  }

  async function handleResetToGlobalDefaults() {
    if (!project) return;
    try {
      setSaving(true);
      const settings = await api.get<GlobalSettings>('/api/settings/defaults');
      setHard({ ...settings.hard_constraints });
      setSoft({ ...settings.soft_constraints });
      setSavedMsg('Loaded global defaults. Click save to apply.');
    } catch(err) {
      setSavedMsg('❌ Failed to load defaults');
    } finally {
      setSaving(false);
      setTimeout(() => setSavedMsg(''), 3000);
    }
  }

  async function handleSaveAsGlobalDefaults() {
    if (!hard || !soft) return;
    try {
      setSaving(true);
      await api.put<GlobalSettings>('/api/settings/defaults', {
        hard_constraints: hard,
        soft_constraints: soft,
      });
      setSavedMsg('✓ Saved as global defaults');
    } catch(err) {
      setSavedMsg('❌ Failed to save global defaults');
    } finally {
      setSaving(false);
      setTimeout(() => setSavedMsg(''), 3000);
    }
  }

  async function handleResetOriginalDefaults() {
    try {
      setSaving(true);
      const settings = await api.post<GlobalSettings>('/api/settings/defaults/reset', {});
      setHard({ ...settings.hard_constraints });
      setSoft({ ...settings.soft_constraints });
      setSavedMsg('✓ Reset to original hardcoded defaults');
    } catch(err) {
      setSavedMsg('❌ Failed to reset original defaults');
    } finally {
      setSaving(false);
      setTimeout(() => setSavedMsg(''), 3000);
    }
  }

  function handleReset() {
    if (mode === 'project' && project) {
      setHard({ ...project.hard_constraints });
      setSoft({ ...project.soft_constraints });
      setProjectName(project.name);
      setSavedMsg('');
    } else if (mode === 'global') {
      fetchGlobalSettings();
      setSavedMsg('');
    }
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
        aria-label="Project Settings"
        aria-modal="true"
      >
        {/* Header */}
        <div className="constraint-header">
          <div className="constraint-header-title">
            <span className="constraint-header-icon">⚙️</span>
            <h2>Project Settings</h2>
          </div>
          <button
            className="constraint-close-btn"
            onClick={onClose}
            aria-label="Close settings panel"
          >
            ✕
          </button>
        </div>

        <div className="constraint-body">
          {/* ── Project General Settings ────────────────────────────────────── */}
          <section className="constraint-section">
            <div className="constraint-section-header">
              <div>
                <h3 className="constraint-section-title">Project Info</h3>
              </div>
            </div>
            
            <div style={{ display: 'flex', gap: '1rem', alignItems: 'flex-end', marginBottom: '1rem' }}>
              <div className="constraint-field" style={{ flex: 1, marginBottom: 0 }}>
                <label className="constraint-label">Project Name</label>
                <div className="constraint-input-row">
                  <input
                    type="text"
                    className="constraint-input"
                    value={projectName}
                    onChange={(e) => setProjectName(e.target.value)}
                  />
                </div>
              </div>
              <button
                className="btn btn-danger"
                onClick={() => {
                  onClose();
                  onDeleteProject();
                }}
              >
                Delete project
              </button>
            </div>

            <div className="constraint-field" style={{ marginBottom: 0 }}>
              <label className="constraint-label">Project Path</label>
              <div className="constraint-input-row" style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
                <input
                  type="text"
                  className="constraint-input constraint-input--readonly"
                  value={project?.project_path ?? ''}
                  readOnly
                  spellCheck={false}
                  title={project?.project_path}
                  onFocus={(e) => e.currentTarget.select()}
                  style={{ flex: 1 }}
                />
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={onOpenProjectFolder}
                  title="Open project folder in file explorer"
                >
                  📂 Open folder
                </button>
              </div>
            </div>
          </section>

          {/* ── Mode Toggle ─────────────────────────────────────────────────── */}
          <section className="constraint-section">
            <div style={{ display: 'flex', gap: '1rem', marginBottom: '1rem' }}>
              <button 
                className={`constraint-btn ${mode === 'project' ? 'constraint-btn--primary' : 'constraint-btn--ghost'}`}
                onClick={() => setMode('project')}
              >
                Project Constraints
              </button>
              <button 
                className={`constraint-btn ${mode === 'global' ? 'constraint-btn--primary' : 'constraint-btn--ghost'}`}
                onClick={() => setMode('global')}
              >
                Global Defaults
              </button>
            </div>
            <p className="constraint-section-desc">
              {mode === 'project' ? 'Editing constraints for this specific project only.' : 'Editing default constraints applied to all newly created projects.'}
            </p>
          </section>

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
          
          {mode === 'project' ? (
            <div className="constraint-footer-actions">
              <button
                className="constraint-btn constraint-btn--ghost"
                type="button"
                onClick={handleResetToGlobalDefaults}
                disabled={saving}
                title="Reset this project's constraints to match the global defaults"
              >
                Reset to Global Defaults
              </button>
              <button
                className="constraint-btn constraint-btn--ghost"
                type="button"
                onClick={handleSaveAsGlobalDefaults}
                disabled={saving}
                title="Save these project constraints as the new global defaults"
              >
                Save as Global Defaults
              </button>
              <div style={{ flex: 1 }}></div>
              <button
                className="constraint-btn constraint-btn--ghost"
                type="button"
                onClick={handleReset}
                disabled={saving}
              >
                Undo Changes
              </button>
              <button
                className="constraint-btn constraint-btn--primary"
                type="button"
                onClick={handleSave}
                disabled={saving}
              >
                {saving ? 'Saving…' : 'Save Project Settings'}
              </button>
            </div>
          ) : (
            <div className="constraint-footer-actions">
              <button
                className="constraint-btn constraint-btn--ghost"
                type="button"
                onClick={handleResetOriginalDefaults}
                disabled={saving}
                title="Reset global defaults to factory original (hardcoded) settings"
              >
                Reset to Original Hardcoded Defaults
              </button>
              <div style={{ flex: 1 }}></div>
              <button
                className="constraint-btn constraint-btn--ghost"
                type="button"
                onClick={handleReset}
                disabled={saving}
              >
                Undo Changes
              </button>
              <button
                className="constraint-btn constraint-btn--primary"
                type="button"
                onClick={handleSave}
                disabled={saving}
              >
                {saving ? 'Saving…' : 'Save Global Defaults'}
              </button>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
