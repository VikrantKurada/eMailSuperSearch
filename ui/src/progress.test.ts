import { describe, expect, it } from "vitest";
import {
  RESULTS_REFRESH_FILES,
  SYNC_POLL_MS,
  progressBaseline,
  shouldPollSync,
  shouldRefreshResults,
} from "./progress";
import type { SyncStatus } from "./types";

const status = (state: SyncStatus["state"], files_pending: number): SyncStatus => ({
  project_id: "p1",
  state,
  last_scan_at: null,
  files_total: 1000,
  files_pending,
  ocr_pending: 0,
  metadata_only: 0,
  last_error: null,
});

describe("sync progress", () => {
  it("polls every 3 s while the sync is not idle", () => {
    expect(SYNC_POLL_MS).toBe(3000);
    expect(shouldPollSync(status("parsing", 500))).toBe(true);
    expect(shouldPollSync(status("scanning", 0))).toBe(true);
    expect(shouldPollSync(status("idle", 0))).toBe(false);
    expect(shouldPollSync(null)).toBe(false);
  });

  it("re-runs the search once 20 more files are indexed", () => {
    expect(RESULTS_REFRESH_FILES).toBe(20);
    expect(shouldRefreshResults(500, status("parsing", 481))).toBe(false);
    expect(shouldRefreshResults(500, status("parsing", 480))).toBe(true);
    expect(shouldRefreshResults(500, status("idle", 0))).toBe(true);
    expect(shouldRefreshResults(null, status("parsing", 0))).toBe(false);
    expect(shouldRefreshResults(500, null)).toBe(false);
    expect(shouldRefreshResults(10, status("parsing", 40))).toBe(false); // a rescan added files
  });

  it("measures progress from the count at the search, set when unknown and raised when a scan adds files", () => {
    expect(progressBaseline(null, status("scanning", 0))).toBe(0); // the search ran before the first status came
    expect(progressBaseline(0, status("parsing", 50_000))).toBe(50_000); // the first scan queued the archive
    expect(progressBaseline(50_000, status("parsing", 49_990))).toBe(50_000);
    expect(progressBaseline(500, null)).toBe(500);
    expect(shouldRefreshResults(progressBaseline(0, status("parsing", 50_000)), status("parsing", 49_975))).toBe(true);
  });
});
