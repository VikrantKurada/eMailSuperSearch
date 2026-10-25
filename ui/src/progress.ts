// When the app polls the sync status and when new progress re-runs the current search. No state and no I/O.
import type { SyncStatus } from "./types";

/** How often the sync status is polled while a sync is running. */
export const SYNC_POLL_MS = 3000;

/** The current search is re-run once this many more files have been indexed since it ran. */
export const RESULTS_REFRESH_FILES = 20;

/** Whether the sync status should be polled: a project is open and its sync is doing something. */
export function shouldPollSync(sync: SyncStatus | null): boolean {
  return sync !== null && sync.state !== "idle";
}

/**
 * The pending file count that progress is measured from: the count when the current search ran, set from `sync` when
 * it was not known then, and raised when a scan has queued more files since.
 */
export function progressBaseline(pendingAtSearch: number | null, sync: SyncStatus | null): number | null {
  if (sync === null) return pendingAtSearch;
  return pendingAtSearch === null ? sync.files_pending : Math.max(pendingAtSearch, sync.files_pending);
}

/**
 * Whether `sync` calls for re-running the current search: the pending file count has dropped by at least
 * `RESULTS_REFRESH_FILES` since the search ran (`pendingAtSearch`, null when it was not known then).
 */
export function shouldRefreshResults(pendingAtSearch: number | null, sync: SyncStatus | null): boolean {
  return pendingAtSearch !== null && sync !== null && pendingAtSearch - sync.files_pending >= RESULTS_REFRESH_FILES;
}
