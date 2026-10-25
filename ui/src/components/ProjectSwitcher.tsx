import { useEffect, useState } from "preact/hooks";
import type { Dispatch } from "preact/hooks";
import type { Api } from "../api";
import { formatAgo } from "../format";
import type { Action } from "../state";
import type { Status, SyncStatus } from "../types";

type Props = {
  status: Status | null;
  projectId: string | null;
  sync: SyncStatus | null;
  dispatch: Dispatch<Action>;
  api: Api;
};

/** The project picker, with the selected project's folder count, email count and last sync time. */
export function ProjectSwitcher({ status, projectId, sync, dispatch, api }: Props) {
  const [folders, setFolders] = useState<number | null>(null);

  useEffect(() => {
    setFolders(null);
    if (projectId === null) return;
    let live = true;
    api
      .project(projectId)
      .then((detail) => live && setFolders(detail.folders.length))
      .catch(() => live && setFolders(null));
    return () => {
      live = false;
    };
  }, [api, projectId]);

  const projects = status?.projects ?? [];
  const parts: string[] = [];
  if (folders !== null) parts.push(folders === 1 ? "1 folder" : `${folders} folders`);
  if (sync !== null) {
    parts.push(`${sync.files_total.toLocaleString()} emails`);
    parts.push(sync.last_scan_at === null ? "not synced yet" : `synced ${formatAgo(sync.last_scan_at)}`);
  }

  return (
    <div class="project-switcher">
      <select
        class="project-select"
        aria-label="Project"
        value={projectId ?? ""}
        onChange={(e) => dispatch({ type: "setProject", projectId: e.currentTarget.value || null })}
      >
        {projects.length === 0 && <option value="">No projects</option>}
        {projects.map((project) => (
          <option key={project.id} value={project.id}>
            {project.name}
          </option>
        ))}
      </select>
      <small class="muted">{parts.join(" · ")}</small>
    </div>
  );
}
