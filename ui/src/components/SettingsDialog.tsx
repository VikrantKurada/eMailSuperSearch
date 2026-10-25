import { useEffect, useState } from "preact/hooks";
import { type Api, ApiError, type SettingsUpdate } from "../api";
import { errorText } from "../format";
import type { ProjectSettings, Settings } from "../types";

type Props = {
  api: Api;
  projectId: string | null;
  onClose: () => void;
  onStopped: () => void; // the host was stopped or purged: the app shows "Host stopped. Close this tab."
};

type Engines = ProjectSettings["ocr_engine_order"];

const ENGINE_ORDERS: [value: string, label: string][] = [
  ["windows,rapid", "Windows OCR, then RapidOCR"],
  ["rapid,windows", "RapidOCR, then Windows OCR"],
  ["windows", "Windows OCR only"],
  ["rapid", "RapidOCR only"],
];

export const PURGE_CONFIRMATION =
  "Delete every project, index and setting of eMailSuperSearch? This cannot be undone.";

/** The host's settings and the selected project's settings, plus Stop host and Purge. */
export function SettingsDialog({ api, projectId, onClose, onStopped }: Props) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [port, setPort] = useState("");
  const [logLevel, setLogLevel] = useState<Settings["log_level"]>("INFO");
  const [dataRoot, setDataRoot] = useState("");
  const [project, setProject] = useState<ProjectSettings | null>(null);
  const [reconcile, setReconcile] = useState("");
  const [ocrEnabled, setOcrEnabled] = useState(true);
  const [engines, setEngines] = useState("windows,rapid");
  const [exportDir, setExportDir] = useState("");
  const [groupDefault, setGroupDefault] = useState(true);
  const [dataRootError, setDataRootError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    api
      .settings()
      .then((s) => {
        if (!live) return;
        setSettings(s);
        setPort(String(s.port));
        setLogLevel(s.log_level);
        setDataRoot(s.data_root);
      })
      .catch((e) => live && setError(errorText(e)));
    if (projectId !== null) {
      api
        .projectSettings(projectId)
        .then((p) => {
          if (!live) return;
          setProject(p);
          setReconcile(String(p.reconcile_interval_s));
          setOcrEnabled(p.ocr_enabled);
          setEngines(p.ocr_engine_order.join(","));
          setExportDir(p.export_dir ?? "");
          setGroupDefault(p.group_by_chain_default);
        })
        .catch((e) => live && setError(errorText(e)));
    }
    return () => {
      live = false;
    };
  }, [api, projectId]);

  const save = async (event: Event) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setDataRootError(null);
    setNote(null);
    const notes: string[] = [];
    try {
      if (settings !== null) {
        // Only what changed is sent: the host re-validates a data root it is given.
        const update: SettingsUpdate = {};
        if (port.trim() !== String(settings.port)) update.port = Number(port);
        if (logLevel !== settings.log_level) update.log_level = logLevel;
        if (dataRoot.trim() !== settings.data_root) update.data_root = dataRoot.trim();
        if (Object.keys(update).length > 0) {
          try {
            const saved = await api.putSettings(update);
            setSettings(saved);
            notes.push(saved.restart_required ? "Saved; the new port or data root applies at the next start." : "Saved.");
          } catch (e) {
            if (e instanceof ApiError && e.status === 400 && update.data_root !== undefined) {
              setDataRootError(e.message);
            } else {
              setError(errorText(e));
            }
            return;
          }
        }
      }
      if (projectId !== null && project !== null) {
        const update: Partial<ProjectSettings> = {};
        if (reconcile.trim() !== String(project.reconcile_interval_s)) update.reconcile_interval_s = Number(reconcile);
        if (ocrEnabled !== project.ocr_enabled) update.ocr_enabled = ocrEnabled;
        if (engines !== project.ocr_engine_order.join(",")) update.ocr_engine_order = engines.split(",") as Engines;
        const dir = exportDir.trim() || null;
        if (dir !== project.export_dir) update.export_dir = dir;
        if (groupDefault !== project.group_by_chain_default) update.group_by_chain_default = groupDefault;
        if (Object.keys(update).length > 0) {
          try {
            setProject(await api.putProjectSettings(projectId, update));
            if (notes.length === 0) notes.push("Saved.");
          } catch (e) {
            setError(errorText(e));
            return;
          }
        }
      }
      setNote(notes.length > 0 ? notes.join(" ") : "Nothing changed.");
    } finally {
      setBusy(false);
    }
  };

  const stopHost = async () => {
    try {
      await api.shutdown();
    } catch (e) {
      // A dropped connection means the host went away before answering: it stopped all the same.
      if (e instanceof ApiError) return setError(e.message);
    }
    onStopped();
  };

  const purge = async () => {
    if (!window.confirm(PURGE_CONFIRMATION)) return;
    try {
      await api.purge();
    } catch (e) {
      if (e instanceof ApiError) return setError(e.message);
    }
    onStopped();
  };

  return (
    <div class="modal-backdrop">
      <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="settings-title">
        <form class="dialog-form" onSubmit={save}>
          <h2 id="settings-title">Settings</h2>

          <fieldset>
            <legend>Host</legend>
            <label class="field">
              <span>Port</span>
              <input
                class="input"
                type="number"
                min={1024}
                max={65535}
                value={port}
                onInput={(e) => setPort(e.currentTarget.value)}
              />
            </label>
            <label class="field">
              <span>Log level</span>
              <select
                class="input"
                value={logLevel}
                onChange={(e) => setLogLevel(e.currentTarget.value as Settings["log_level"])}
              >
                {["DEBUG", "INFO", "WARNING", "ERROR"].map((level) => (
                  <option key={level} value={level}>
                    {level}
                  </option>
                ))}
              </select>
            </label>
            <label class="field">
              <span>Data root</span>
              <input class="input" type="text" value={dataRoot} onInput={(e) => setDataRoot(e.currentTarget.value)} />
              {dataRootError && <span class="error-text">{dataRootError}</span>}
            </label>
            <label class="switch-row disabled">
              <span>Start with Windows · Coming in the hardening milestone</span>
              <input type="checkbox" class="switch" checked={false} disabled />
            </label>
          </fieldset>

          {projectId !== null && (
            <fieldset disabled={project === null}>
              <legend>This project</legend>
              <label class="field">
                <span>Reconcile interval (seconds)</span>
                <input
                  class="input"
                  type="number"
                  min={60}
                  max={86400}
                  value={reconcile}
                  onInput={(e) => setReconcile(e.currentTarget.value)}
                />
              </label>
              <label class="switch-row">
                <span>OCR enabled</span>
                <input
                  type="checkbox"
                  class="switch"
                  checked={ocrEnabled}
                  onChange={(e) => setOcrEnabled(e.currentTarget.checked)}
                />
              </label>
              <label class="field">
                <span>OCR engine order</span>
                <select class="input" value={engines} onChange={(e) => setEngines(e.currentTarget.value)}>
                  {ENGINE_ORDERS.map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <label class="field">
                <span>Export folder (empty for the project's own)</span>
                <input
                  class="input"
                  type="text"
                  value={exportDir}
                  onInput={(e) => setExportDir(e.currentTarget.value)}
                />
              </label>
              <label class="switch-row">
                <span>Group by chain by default</span>
                <input
                  type="checkbox"
                  class="switch"
                  checked={groupDefault}
                  onChange={(e) => setGroupDefault(e.currentTarget.checked)}
                />
              </label>
            </fieldset>
          )}

          {error && <p class="error-text">{error}</p>}
          {note && <p class="muted">{note}</p>}

          <div class="dialog-actions">
            <button type="button" class="btn danger" onClick={purge}>
              Purge
            </button>
            <button type="button" class="btn" onClick={stopHost}>
              Stop host
            </button>
            <span class="spacer" />
            <button type="button" class="btn" onClick={onClose}>
              Close
            </button>
            <button type="submit" class="btn primary" disabled={busy || settings === null}>
              Save
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
