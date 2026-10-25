import { useState } from "preact/hooks";
import type { Api } from "../api";
import { errorText } from "../format";
import type { Project } from "../types";

type Props = {
  api: Api;
  heading: string;
  onCreated: (project: Project, warning: string | null) => void;
  onCancel?: () => void; // absent in the first-run flow, where there is nothing to go back to
};

/** Create a project: its name, the folders to watch (typed or picked with Browse) and whether to OCR. */
export function ProjectDialog({ api, heading, onCreated, onCancel }: Props) {
  const [name, setName] = useState("");
  const [folders, setFolders] = useState<string[]>([]);
  const [draft, setDraft] = useState("");
  const [ocr, setOcr] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const addFolder = (path: string) => {
    const folder = path.trim();
    if (folder && !folders.includes(folder)) setFolders([...folders, folder]);
  };

  const browse = async () => {
    setError(null);
    try {
      const { path } = await api.pickFolder();
      if (path) addFolder(path);
    } catch (e) {
      setError(errorText(e));
    }
  };

  const create = async (event: Event) => {
    event.preventDefault();
    // A typed path not yet added with Add still counts.
    const all = draft.trim() && !folders.includes(draft.trim()) ? [...folders, draft.trim()] : folders;
    if (!name.trim()) return setError("Give the project a name.");
    if (all.length === 0) return setError("Add at least one folder.");
    setBusy(true);
    setError(null);
    let project: Project;
    try {
      // The OCR choice travels with the create, so the project's first scan already honours it.
      project = await api.createProject({ name: name.trim(), folders: all, ocr_enabled: ocr });
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
      return;
    }
    onCreated(project, null);
  };

  return (
    <div class="modal-backdrop">
      <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="project-dialog-title">
        <form class="dialog-form" onSubmit={create}>
          <h2 id="project-dialog-title">{heading}</h2>

          <label class="field">
            <span>Name</span>
            <input class="input" type="text" value={name} onInput={(e) => setName(e.currentTarget.value)} />
          </label>

          <div class="field">
            <span>Folders</span>
            <ul class="folder-list">
              {folders.map((folder) => (
                <li key={folder}>
                  <span class="folder-path">{folder}</span>
                  <button
                    type="button"
                    class="chip-remove"
                    aria-label={`Remove ${folder}`}
                    onClick={() => setFolders(folders.filter((f) => f !== folder))}
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
            <div class="folder-add">
              <input
                class="input"
                type="text"
                placeholder="C:\Mail\Archive"
                aria-label="Folder path"
                value={draft}
                onInput={(e) => setDraft(e.currentTarget.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    addFolder(draft);
                    setDraft("");
                  }
                }}
              />
              <button
                type="button"
                class="btn"
                onClick={() => {
                  addFolder(draft);
                  setDraft("");
                }}
              >
                Add
              </button>
              <button type="button" class="btn" onClick={browse}>
                Browse…
              </button>
            </div>
          </div>

          <label class="switch-row">
            <span>Recover text from scanned attachments (OCR)</span>
            <input type="checkbox" class="switch" checked={ocr} onChange={(e) => setOcr(e.currentTarget.checked)} />
          </label>

          {error && <p class="error-text">{error}</p>}

          <div class="dialog-actions">
            <span class="spacer" />
            {onCancel && (
              <button type="button" class="btn" onClick={onCancel}>
                Cancel
              </button>
            )}
            <button type="submit" class="btn primary" disabled={busy}>
              Create project
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
