import { useEffect, useState } from "preact/hooks";
import type { Dispatch } from "preact/hooks";
import type { Action } from "../state";
import type { SearchFilters } from "../types";

type Props = { filters: SearchFilters; dispatch: Dispatch<Action> };

/** The query field: it searches on Enter, and follows the query when a saved search replaces the filters. */
export function SearchBox({ filters, dispatch }: Props) {
  const [draft, setDraft] = useState(filters.query);

  useEffect(() => setDraft(filters.query), [filters.query]);

  return (
    <input
      class="input search-input"
      type="search"
      placeholder="Search emails and attachments"
      aria-label="Search"
      value={draft}
      onInput={(e) => setDraft(e.currentTarget.value)}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          dispatch({ type: "setFilters", filters: { ...filters, query: draft.trim() } });
        }
      }}
    />
  );
}
