import { useEffect, useMemo, useReducer, useRef, useState } from "preact/hooks";
import { Api } from "./api";
import { LeftPane } from "./components/LeftPane";
import { MessageViewer } from "./components/MessageViewer";
import { PlaceholderTab } from "./components/PlaceholderTab";
import { ProjectDialog } from "./components/ProjectDialog";
import { ResultsTab } from "./components/ResultsTab";
import { SettingsDialog } from "./components/SettingsDialog";
import { Tabs } from "./components/Tabs";
import { errorText } from "./format";
import { SYNC_POLL_MS, progressBaseline, shouldPollSync, shouldRefreshResults } from "./progress";
import { initialState, reducer } from "./state";
import type { Project, SyncStatus } from "./types";

/** The sync events after which the status line refetches the project's sync status and the host status. */
const REFRESH_EVENTS = ["scan_started", "scan_finished", "ocr_done", "error"];

/** Layout A: the left search pane and the tabbed main area, plus the viewer drawer and the dialogs. */
export function App({ api: given }: { api?: Api }) {
  const api = useMemo(() => given ?? new Api(), [given]);
  const [state, dispatch] = useReducer(reducer, initialState);
  const [sync, setSync] = useState<SyncStatus | null>(null);
  const [dialog, setDialog] = useState<"project" | "settings" | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [stopped, setStopped] = useState(false);
  const filters = useRef(state.filters);
  filters.current = state.filters;
  const searchSeq = useRef(0);
  const searchFailed = useRef(false);
  // Re-running the current search as indexing progresses: a bump of `rerun` repeats it; `pendingAtSearch` holds the
  // pending file count when it last ran.
  const [rerun, setRerun] = useState(0);
  const pendingAtSearch = useRef<number | null>(null);
  const syncNow = useRef<SyncStatus | null>(null);
  syncNow.current = sync;
  const { projectId } = state;
  const polling = shouldPollSync(sync);

  // The host status on mount; the first project is selected when there is one.
  useEffect(() => {
    api
      .status()
      .then((status) => {
        dispatch({ type: "setStatus", status });
        if (status.projects.length > 0) dispatch({ type: "setProject", projectId: status.projects[0].id });
      })
      .catch((e) => dispatch({ type: "setError", error: errorText(e) }))
      .finally(() => setLoaded(true));
  }, [api]);

  // The selected project's progress stream; closed when the project changes, the host stops or the app unmounts.
  useEffect(() => {
    setSync(null);
    if (projectId === null || stopped) return;
    let live = true;
    const refresh = () => {
      api
        .syncStatus(projectId)
        .then((s) => live && setSync(s))
        .catch(() => undefined); // a blocked project has no sync status; the status line says so
      api
        .status()
        .then((status) => live && dispatch({ type: "setStatus", status }))
        .catch(() => undefined);
    };
    refresh();
    const source = api.syncEvents(projectId);
    source.addEventListener("status", (event) => {
      if (live) setSync(JSON.parse((event as MessageEvent<string>).data) as SyncStatus);
    });
    for (const name of REFRESH_EVENTS) source.addEventListener(name, refresh);
    // A finished scan has indexed what it found: show it in the results.
    source.addEventListener("scan_finished", () => live && setRerun((n) => n + 1));
    return () => {
      live = false;
      source.close();
    };
  }, [api, projectId, stopped]);

  // While a sync runs (the first index of a large folder takes hours), poll its status every 3 s so the counts move.
  useEffect(() => {
    if (projectId === null || stopped || !polling) return;
    let live = true;
    const timer = setInterval(() => {
      api
        .syncStatus(projectId)
        .then((s) => live && setSync(s))
        .catch(() => undefined);
    }, SYNC_POLL_MS);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [api, projectId, stopped, polling]);

  // Re-run the current search once enough files have been indexed since it ran, so results fill in as indexing goes.
  useEffect(() => {
    if (shouldRefreshResults(pendingAtSearch.current, sync)) setRerun((n) => n + 1);
    else pendingAtSearch.current = progressBaseline(pendingAtSearch.current, sync);
  }, [sync]);

  // The project's group-by-chain default applies when it is opened.
  useEffect(() => {
    if (projectId === null || stopped) return;
    let live = true;
    api
      .projectSettings(projectId)
      .then((settings) => {
        const current = filters.current;
        if (live && settings.group_by_chain_default !== current.group_by_chain) {
          dispatch({ type: "setFilters", filters: { ...current, group_by_chain: settings.group_by_chain_default } });
        }
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [api, projectId, stopped]);

  // Search whenever the project, the filters (the query on Enter, the sort included) or the page change, and again
  // when indexing progress calls for it (`rerun`).
  useEffect(() => {
    if (projectId === null || stopped) return;
    const seq = ++searchSeq.current;
    pendingAtSearch.current = syncNow.current?.files_pending ?? null;
    dispatch({ type: "setBusy", busy: true });
    api
      .search(projectId, state.filters, state.page)
      .then((results) => {
        if (seq !== searchSeq.current) return; // a newer search is running: its answer wins
        dispatch({ type: "setResults", results });
        if (searchFailed.current) dispatch({ type: "setError", error: null });
        searchFailed.current = false;
      })
      .catch((e) => {
        if (seq !== searchSeq.current) return;
        searchFailed.current = true;
        dispatch({ type: "setError", error: errorText(e) });
      })
      .finally(() => {
        if (seq === searchSeq.current) dispatch({ type: "setBusy", busy: false });
      });
  }, [api, projectId, state.filters, state.page, stopped, rerun]);

  const created = async (project: Project, warning: string | null) => {
    setDialog(null);
    try {
      dispatch({ type: "setStatus", status: await api.status() });
    } catch (e) {
      dispatch({ type: "setError", error: errorText(e) });
    }
    dispatch({ type: "setProject", projectId: project.id });
    if (warning) dispatch({ type: "setError", error: warning });
  };

  if (stopped) {
    return <div class="stopped">Host stopped. Close this tab.</div>;
  }

  const firstRun = loaded && state.status !== null && state.status.projects.length === 0;

  return (
    <div class="app">
      <LeftPane
        state={state}
        dispatch={dispatch}
        api={api}
        sync={sync}
        onNewProject={() => setDialog("project")}
        onSettings={() => setDialog("settings")}
      />
      <main class="main">
        <Tabs tab={state.tab} dispatch={dispatch} />
        <div class="tab-body">
          {state.tab === "results" ? <ResultsTab state={state} dispatch={dispatch} api={api} /> : <PlaceholderTab />}
        </div>
      </main>
      {state.selectedKey !== null && projectId !== null && (
        <MessageViewer projectId={projectId} messageKey={state.selectedKey} api={api} dispatch={dispatch} />
      )}
      {firstRun && <ProjectDialog api={api} heading="Create your first project" onCreated={created} />}
      {!firstRun && dialog === "project" && (
        <ProjectDialog api={api} heading="New project" onCreated={created} onCancel={() => setDialog(null)} />
      )}
      {dialog === "settings" && (
        <SettingsDialog
          api={api}
          projectId={projectId}
          onClose={() => setDialog(null)}
          onStopped={() => setStopped(true)}
        />
      )}
    </div>
  );
}
