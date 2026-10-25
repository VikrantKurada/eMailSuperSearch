import type { SearchFilters, SearchResponse, Status } from "./types";

export type Tab = "results" | "chat" | "graph" | "artefacts";

export type State = {
  projectId: string | null;
  filters: SearchFilters;
  page: number;
  results: SearchResponse | null;
  selectedKey: string | null;
  pinnedKeys: Set<string>;
  pinnedChainIds: Set<string>;
  tab: Tab;
  status: Status | null;
  busy: boolean;
  error: string | null;
};

export type Action =
  | { type: "setProject"; projectId: string | null }
  | { type: "setFilters"; filters: SearchFilters }
  | { type: "setPage"; page: number }
  | { type: "setResults"; results: SearchResponse | null }
  | { type: "select"; key: string | null }
  | { type: "togglePinKey"; key: string }
  | { type: "togglePinChain"; chainId: string }
  | { type: "clearPins" }
  | { type: "setTab"; tab: Tab }
  | { type: "setStatus"; status: Status | null }
  | { type: "setBusy"; busy: boolean }
  | { type: "setError"; error: string | null };

export const initialState: State = {
  projectId: null,
  filters: {
    query: "",
    from_addrs: [],
    recipient_addrs: [],
    date_from: null,
    date_to: null,
    attachment_kinds: [],
    has_attachments: null,
    group_by_chain: true,
    only_with_conclusion: false,
    sort: "relevance",
  },
  page: 0,
  results: null,
  selectedKey: null,
  pinnedKeys: new Set(),
  pinnedChainIds: new Set(),
  tab: "results",
  status: null,
  busy: false,
  error: null,
};

function toggled(set: Set<string>, value: string): Set<string> {
  const next = new Set(set);
  if (!next.delete(value)) next.add(value);
  return next;
}

export function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "setProject":
      return {
        ...state,
        projectId: action.projectId,
        results: null,
        selectedKey: null,
        pinnedKeys: new Set(),
        pinnedChainIds: new Set(),
        page: 0,
        error: null,
      };
    case "setFilters":
      return { ...state, filters: action.filters, page: 0 };
    case "setPage":
      return { ...state, page: action.page };
    case "setResults":
      return { ...state, results: action.results };
    case "select":
      return { ...state, selectedKey: action.key };
    case "togglePinKey":
      return { ...state, pinnedKeys: toggled(state.pinnedKeys, action.key) };
    case "togglePinChain":
      return { ...state, pinnedChainIds: toggled(state.pinnedChainIds, action.chainId) };
    case "clearPins":
      return { ...state, pinnedKeys: new Set(), pinnedChainIds: new Set() };
    case "setTab":
      return { ...state, tab: action.tab };
    case "setStatus":
      return { ...state, status: action.status };
    case "setBusy":
      return { ...state, busy: action.busy };
    case "setError":
      return { ...state, error: action.error };
  }
}

/**
 * The JSON body the search route takes for `filters` on `page`. `query`, `group_by_chain`, `sort` and `page` are
 * always sent; a list only when it has items; `date_from`, `date_to` and `has_attachments` only when not null; and
 * `only_with_conclusion` only when set (the host's default is off). Anything left out takes the host's default.
 */
export function filtersToBody(filters: SearchFilters, page: number): Record<string, unknown> {
  const body: Record<string, unknown> = {
    query: filters.query,
    group_by_chain: filters.group_by_chain,
    sort: filters.sort,
    page,
  };
  if (filters.from_addrs.length > 0) body.from_addrs = filters.from_addrs;
  if (filters.recipient_addrs.length > 0) body.recipient_addrs = filters.recipient_addrs;
  if (filters.attachment_kinds.length > 0) body.attachment_kinds = filters.attachment_kinds;
  if (filters.date_from !== null) body.date_from = filters.date_from;
  if (filters.date_to !== null) body.date_to = filters.date_to;
  if (filters.has_attachments !== null) body.has_attachments = filters.has_attachments;
  if (filters.only_with_conclusion) body.only_with_conclusion = true;
  return body;
}
