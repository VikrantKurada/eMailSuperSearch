import type { Dispatch } from "preact/hooks";
import type { Api } from "../api";
import type { Action, State } from "../state";
import type { SyncStatus } from "../types";
import { Filters } from "./Filters";
import { ProjectSwitcher } from "./ProjectSwitcher";
import { SavedSearches } from "./SavedSearches";
import { SearchBox } from "./SearchBox";
import { StatusLine } from "./StatusLine";

type Props = {
  state: State;
  dispatch: Dispatch<Action>;
  api: Api;
  sync: SyncStatus | null;
  onNewProject: () => void;
  onSettings: () => void;
};

/** The left search pane of layout A. */
export function LeftPane({ state, dispatch, api, sync, onNewProject, onSettings }: Props) {
  return (
    <aside class="left-pane">
      <ProjectSwitcher status={state.status} projectId={state.projectId} sync={sync} dispatch={dispatch} api={api} />

      <h3 class="pane-heading">Search</h3>
      <SearchBox filters={state.filters} dispatch={dispatch} />

      <Filters filters={state.filters} dispatch={dispatch} />

      <SavedSearches api={api} projectId={state.projectId} filters={state.filters} dispatch={dispatch} />

      <StatusLine
        status={state.status}
        sync={sync}
        projectId={state.projectId}
        busy={state.busy}
        error={state.error}
        api={api}
        dispatch={dispatch}
      />

      <div class="pane-footer">
        <button type="button" class="btn" onClick={onNewProject}>
          + New project
        </button>
        <button type="button" class="btn" onClick={onSettings}>
          Settings
        </button>
      </div>
    </aside>
  );
}
