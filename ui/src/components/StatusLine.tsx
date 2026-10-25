import type { Dispatch } from "preact/hooks";
import type { Api } from "../api";
import { errorText } from "../format";
import type { Action } from "../state";
import type { Status, SyncStatus } from "../types";

type Props = {
  status: Status | null;
  sync: SyncStatus | null;
  projectId: string | null;
  busy: boolean;
  error: string | null;
  api: Api;
  dispatch: Dispatch<Action>;
};

const DEGRADED_BADGES: Record<string, string> = { embedder: "No semantic search", ocr: "No OCR" };

/** The sync state and counts, the degraded-mode badges, the last error, and Sync now. Never a modal. */
export function StatusLine({ status, sync, projectId, busy, error, api, dispatch }: Props) {
  const blocked = status?.projects.find((p) => p.id === projectId)?.blocked ?? null;

  const syncNow = () => {
    if (projectId === null) return;
    api.syncNow(projectId).catch((e) => dispatch({ type: "setError", error: errorText(e) }));
  };

  const counts: string[] = [];
  if (sync !== null) {
    counts.push(`${sync.files_total.toLocaleString()} files`);
    if (sync.files_pending > 0) counts.push(`${sync.files_pending.toLocaleString()} pending`);
    if (sync.ocr_pending > 0) counts.push(`${sync.ocr_pending.toLocaleString()} awaiting OCR`);
  }

  return (
    <div class="status-line" role="status">
      <div class="status-row">
        <span class={`dot ${sync === null ? "off" : sync.state === "idle" ? "idle" : "busy"}`} />
        <span class="sync-state">{blocked !== null ? "not open" : (sync?.state ?? "no project")}</span>
        {counts.length > 0 && <span class="muted"> · {counts.join(" · ")}</span>}
      </div>
      <div class="badges">
        {(status?.degraded ?? []).map((part) => (
          <span class="badge warn" key={part}>
            {DEGRADED_BADGES[part] ?? `No ${part}`}
          </span>
        ))}
        {sync !== null && sync.metadata_only > 0 && (
          <span class="badge" title="Messages whose text could not be extracted; they still match on headers">
            {sync.metadata_only.toLocaleString()} metadata-only
          </span>
        )}
      </div>
      {blocked !== null && <div class="error-text">The project is not open: {blocked}</div>}
      {sync?.last_error && <div class="error-text">Last sync error: {sync.last_error}</div>}
      {busy && <div class="muted">Searching…</div>}
      {error && (
        <div class="error-text">
          {error}{" "}
          <button type="button" class="link" onClick={() => dispatch({ type: "setError", error: null })}>
            Dismiss
          </button>
        </div>
      )}
      <button type="button" class="btn" disabled={projectId === null || blocked !== null} onClick={syncNow}>
        Sync now
      </button>
    </div>
  );
}
