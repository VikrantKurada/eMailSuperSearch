import { useRef, useState } from "preact/hooks";
import type { Dispatch } from "preact/hooks";
import type { Api } from "../api";
import { errorText } from "../format";
import type { Action, State } from "../state";
import type { ExportInfo } from "../types";
import { ChainCard } from "./ChainCard";
import { MessageCard } from "./MessageCard";

type Props = { state: State; dispatch: Dispatch<Action>; api: Api };

/** The Results tab: the header (counts, sort, pins, export), the chain or message cards and the pager. */
export function ResultsTab({ state, dispatch, api }: Props) {
  const { results, filters, projectId, pinnedKeys, pinnedChainIds } = state;
  const [exported, setExported] = useState<ExportInfo | null>(null);
  const [exporting, setExporting] = useState(false);
  const download = useRef<HTMLAnchorElement>(null);
  const pinned = pinnedKeys.size + pinnedChainIds.size;

  const setSort = (sort: "relevance" | "date") => {
    if (sort !== filters.sort) dispatch({ type: "setFilters", filters: { ...filters, sort } });
  };

  const exportExcel = async () => {
    if (projectId === null) return;
    setExporting(true);
    try {
      const info = await api.exportExcel(projectId, filters, pinnedKeys, pinnedChainIds);
      setExported(info);
      const anchor = download.current;
      if (anchor) {
        anchor.href = api.exportUrl(projectId, info.name);
        anchor.click();
      }
    } catch (e) {
      dispatch({ type: "setError", error: errorText(e) });
    } finally {
      setExporting(false);
    }
  };

  // The results may still be those of the previous filters while a search runs: go by what they hold.
  const grouped = results !== null && results.messages.length === 0;
  // A query's totals count its top matches; filters alone count every matching email.
  const matchLabel = filters.query.trim() === "" ? "emails match" : "top matches";
  const total = results === null ? 0 : grouped ? results.total_chains : results.total_messages;
  const pages = results === null ? 0 : Math.max(1, Math.ceil(total / results.page_size));

  return (
    <section class="results">
      <header class="results-header">
        <span class="muted">
          {results === null
            ? state.busy
              ? "Searching…"
              : ""
            : `${results.total_chains} chains · ${results.total_messages} ${matchLabel}`}
        </span>
        <div class="results-tools">
          <div class="segmented" role="group" aria-label="Sort by">
            <button
              type="button"
              aria-pressed={filters.sort === "relevance" ? "true" : "false"}
              class={filters.sort === "relevance" ? "on" : ""}
              onClick={() => setSort("relevance")}
            >
              Relevance
            </button>
            <button
              type="button"
              aria-pressed={filters.sort === "date" ? "true" : "false"}
              class={filters.sort === "date" ? "on" : ""}
              onClick={() => setSort("date")}
            >
              Date
            </button>
          </div>
          {pinned > 0 && (
            <span class="pinned">
              {pinned} pinned{" "}
              <button type="button" class="link" onClick={() => dispatch({ type: "clearPins" })}>
                Clear
              </button>
            </span>
          )}
          <button
            type="button"
            class="btn"
            disabled={projectId === null || exporting}
            onClick={exportExcel}
          >
            Export to Excel
          </button>
          <a ref={download} class="hidden" aria-hidden="true" tabIndex={-1} download />
        </div>
      </header>
      {exported && (
        <p class="export-note muted">
          Exported {exported.rows} {exported.rows === 1 ? "row" : "rows"} to {exported.name}.
          {exported.truncated && " More emails match than the export holds (the row limit or the top-matches cap)."}
        </p>
      )}

      {results !== null && results.total_messages === 0 && <p class="empty muted">No emails match this search.</p>}

      {results !== null &&
        projectId !== null &&
        (grouped
          ? results.chains.map((chain) => (
              <ChainCard
                key={chain.chain_id}
                chain={chain}
                projectId={projectId}
                api={api}
                dispatch={dispatch}
                pinnedChainIds={pinnedChainIds}
                pinnedKeys={pinnedKeys}
              />
            ))
          : results.messages.map((message) => (
              <MessageCard
                key={message.message_key}
                message={message}
                projectId={projectId}
                api={api}
                dispatch={dispatch}
                pinned={pinnedKeys.has(message.message_key)}
              />
            )))}

      {results !== null && pages > 1 && (
        <nav class="pager" aria-label="Pages">
          <button
            type="button"
            class="btn"
            disabled={state.page === 0}
            onClick={() => dispatch({ type: "setPage", page: state.page - 1 })}
          >
            Previous
          </button>
          <span class="muted">
            Page {state.page + 1} of {pages}
          </span>
          <button
            type="button"
            class="btn"
            disabled={state.page + 1 >= pages}
            onClick={() => dispatch({ type: "setPage", page: state.page + 1 })}
          >
            Next
          </button>
        </nav>
      )}
    </section>
  );
}
