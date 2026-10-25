import { filtersToBody } from "./state";
import type {
  ExportInfo,
  MessageDetail,
  Project,
  ProjectSettings,
  SavedSearch,
  SearchFilters,
  SearchResponse,
  Settings,
  Status,
  SyncStatus,
} from "./types";

/** A non-2xx answer: `status` is the HTTP status and `message` the host's `error` text (else the status text). */
export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** The CSRF token: the host sets it in the readable cookie `ess_csrf`; every non-GET request echoes it. */
export function csrfToken(): string {
  for (const part of document.cookie.split(";")) {
    const [name, ...value] = part.trim().split("=");
    if (name === "ess_csrf") return value.join("=");
  }
  return "";
}

export type SettingsUpdate = Partial<Pick<Settings, "port" | "log_level" | "data_root">>;

const projectPath = (id: string) => `/api/projects/${encodeURIComponent(id)}`;
const messagePath = (id: string, key: string) => `${projectPath(id)}/messages/${encodeURIComponent(key)}`;

/** The host's JSON API. Paths are relative: the UI is always served by the host itself, same origin. */
export class Api {
  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const headers: Record<string, string> = {};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (method !== "GET") headers["X-ESS-Token"] = csrfToken();
    const response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) {
      const failure = await response.json().catch(() => null);
      throw new ApiError(response.status, failure?.error ?? response.statusText);
    }
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }

  status(): Promise<Status> {
    return this.request("GET", "/api/status");
  }

  settings(): Promise<Settings> {
    return this.request("GET", "/api/settings");
  }

  putSettings(body: SettingsUpdate): Promise<Settings & { restart_required: boolean }> {
    return this.request("PUT", "/api/settings", body);
  }

  projects(): Promise<Project[]> {
    return this.request("GET", "/api/projects");
  }

  createProject(body: { name: string; folders: string[]; language: string }): Promise<Project> {
    return this.request("POST", "/api/projects", body);
  }

  deleteProject(id: string): Promise<void> {
    return this.request("DELETE", projectPath(id));
  }

  projectSettings(id: string): Promise<ProjectSettings> {
    return this.request("GET", `${projectPath(id)}/settings`);
  }

  putProjectSettings(id: string, body: Partial<ProjectSettings>): Promise<ProjectSettings> {
    return this.request("PUT", `${projectPath(id)}/settings`, body);
  }

  search(projectId: string, filters: SearchFilters, page: number, pageSize = 20): Promise<SearchResponse> {
    return this.request("POST", `${projectPath(projectId)}/search`, {
      ...filtersToBody(filters, page),
      page_size: pageSize,
    });
  }

  message(projectId: string, key: string): Promise<MessageDetail> {
    return this.request("GET", messagePath(projectId, key));
  }

  attachmentUrl(projectId: string, key: string, ordinal: number): string {
    return `${messagePath(projectId, key)}/attachments/${ordinal}`;
  }

  openInOutlook(projectId: string, key: string): Promise<void> {
    return this.request("POST", `${messagePath(projectId, key)}/open`);
  }

  reveal(projectId: string, key: string): Promise<void> {
    return this.request("POST", `${messagePath(projectId, key)}/reveal`);
  }

  /** Export the pinned messages and chains when there are any, else every result of `filters` (the host decides). */
  exportExcel(
    projectId: string,
    filters: SearchFilters,
    pinnedKeys: Iterable<string>,
    pinnedChainIds: Iterable<string>,
  ): Promise<ExportInfo> {
    return this.request("POST", `${projectPath(projectId)}/export/excel`, {
      filters: filtersToBody(filters, 0), // the host ignores `page` here
      pinned_keys: [...pinnedKeys],
      pinned_chain_ids: [...pinnedChainIds],
    });
  }

  exportUrl(projectId: string, name: string): string {
    return `${projectPath(projectId)}/exports/${encodeURIComponent(name)}`;
  }

  syncNow(projectId: string): Promise<{ accepted: boolean }> {
    return this.request("POST", `${projectPath(projectId)}/sync`);
  }

  syncStatus(projectId: string): Promise<SyncStatus> {
    return this.request("GET", `${projectPath(projectId)}/sync/status`);
  }

  /** The progress stream: a `status` event first, then `scan_started`, `scan_finished`, `ocr_done`, `error`, ... */
  syncEvents(projectId: string): EventSource {
    return new EventSource(`${projectPath(projectId)}/sync/events`);
  }

  pickFolder(): Promise<{ path: string | null }> {
    return this.request("POST", "/api/pick-folder");
  }

  savedSearches(projectId: string): Promise<SavedSearch[]> {
    return this.request("GET", `${projectPath(projectId)}/saved-searches`);
  }

  putSavedSearches(projectId: string, list: SavedSearch[]): Promise<SavedSearch[]> {
    return this.request("PUT", `${projectPath(projectId)}/saved-searches`, list);
  }

  purge(): Promise<{ accepted: boolean }> {
    return this.request("POST", "/api/purge");
  }

  shutdown(): Promise<void> {
    return this.request("POST", "/api/shutdown");
  }
}
