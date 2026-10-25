import { useEffect, useState } from "preact/hooks";
import type { Dispatch } from "preact/hooks";
import type { Api } from "../api";
import { errorText } from "../format";
import { type Action, initialState } from "../state";
import type { SavedSearch, SearchFilters } from "../types";

type Props = { api: Api; projectId: string | null; filters: SearchFilters; dispatch: Dispatch<Action> };

/** The project's saved searches: clicking one applies its filters; Save current stores the current ones by name. */
export function SavedSearches({ api, projectId, filters, dispatch }: Props) {
  const [list, setList] = useState<SavedSearch[]>([]);

  useEffect(() => {
    setList([]);
    if (projectId === null) return;
    let live = true;
    api
      .savedSearches(projectId)
      .then((saved) => live && setList(saved))
      .catch((e) => live && dispatch({ type: "setError", error: errorText(e) }));
    return () => {
      live = false;
    };
  }, [api, projectId, dispatch]);

  const save = async () => {
    if (projectId === null) return;
    const name = window.prompt("Name this search")?.trim();
    if (!name) return;
    const next = [...list.filter((s) => s.name !== name), { name, filters }];
    try {
      setList(await api.putSavedSearches(projectId, next));
    } catch (e) {
      dispatch({ type: "setError", error: errorText(e) });
    }
  };

  return (
    <div class="saved-searches">
      <div class="pane-heading-row">
        <h3 class="pane-heading">Saved searches</h3>
        <button type="button" class="link" disabled={projectId === null} onClick={save}>
          Save current
        </button>
      </div>
      {list.length === 0 && <p class="muted small">None yet</p>}
      <ul>
        {list.map((saved) => (
          <li key={saved.name}>
            <button
              type="button"
              class="saved-item"
              // A saved search from an older version may lack newer fields: the defaults fill them.
              onClick={() => dispatch({ type: "setFilters", filters: { ...initialState.filters, ...saved.filters } })}
            >
              {saved.name}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
