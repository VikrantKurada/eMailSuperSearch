import { describe, expect, it } from "vitest";
import { initialState, reducer } from "./state";
import type { SearchResponse } from "./types";

const response: SearchResponse = {
  total_messages: 1,
  total_chains: 0,
  page: 0,
  page_size: 20,
  chains: [],
  messages: [],
  mode: "hybrid",
};

describe("reducer", () => {
  it("setFilters resets page to 0", () => {
    const onPage3 = reducer(initialState, { type: "setPage", page: 3 });
    expect(onPage3.page).toBe(3);
    const next = reducer(onPage3, { type: "setFilters", filters: { ...onPage3.filters, query: "budget" } });
    expect(next.filters.query).toBe("budget");
    expect(next.page).toBe(0);
  });

  it("setProject clears results, selection and pins", () => {
    let state = reducer(initialState, { type: "setProject", projectId: "p1" });
    state = reducer(state, { type: "setResults", results: response });
    state = reducer(state, { type: "select", key: "k1" });
    state = reducer(state, { type: "togglePinKey", key: "k1" });
    state = reducer(state, { type: "togglePinChain", chainId: "c1" });
    state = reducer(state, { type: "setPage", page: 2 });
    state = reducer(state, { type: "setError", error: "boom" });
    expect(state.results).toBe(response);
    expect(state.pinnedKeys.size).toBe(1);

    const next = reducer(state, { type: "setProject", projectId: "p2" });
    expect(next.projectId).toBe("p2");
    expect(next.results).toBeNull();
    expect(next.selectedKey).toBeNull();
    expect(next.pinnedKeys.size).toBe(0);
    expect(next.pinnedChainIds.size).toBe(0);
    expect(next.page).toBe(0);
    expect(next.error).toBeNull();
    expect(next.filters).toBe(state.filters);
  });

  it("togglePinKey adds then removes", () => {
    const added = reducer(initialState, { type: "togglePinKey", key: "k1" });
    expect(added.pinnedKeys.has("k1")).toBe(true);
    expect(initialState.pinnedKeys.has("k1")).toBe(false); // the old state is not mutated
    const removed = reducer(added, { type: "togglePinKey", key: "k1" });
    expect(removed.pinnedKeys.has("k1")).toBe(false);
  });

  it("togglePinChain adds then removes, and clearPins empties both sets", () => {
    const added = reducer(initialState, { type: "togglePinChain", chainId: "c1" });
    expect(added.pinnedChainIds.has("c1")).toBe(true);
    expect(reducer(added, { type: "togglePinChain", chainId: "c1" }).pinnedChainIds.size).toBe(0);
    const both = reducer(added, { type: "togglePinKey", key: "k1" });
    const cleared = reducer(both, { type: "clearPins" });
    expect(cleared.pinnedKeys.size).toBe(0);
    expect(cleared.pinnedChainIds.size).toBe(0);
  });

  it("the simple setters change only their own field", () => {
    expect(reducer(initialState, { type: "select", key: "k9" }).selectedKey).toBe("k9");
    expect(reducer(initialState, { type: "setTab", tab: "graph" }).tab).toBe("graph");
    expect(reducer(initialState, { type: "setBusy", busy: true }).busy).toBe(true);
    expect(reducer(initialState, { type: "setError", error: "x" }).error).toBe("x");
    const status = { version: "1", degraded: [], projects: [] };
    expect(reducer(initialState, { type: "setStatus", status }).status).toBe(status);
  });
});
