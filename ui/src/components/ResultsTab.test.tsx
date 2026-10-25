import { cleanup, fireEvent, render, screen } from "@testing-library/preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Api } from "../api";
import { initialState, type State } from "../state";
import type { ChainCard, SearchResponse } from "../types";
import { ResultsTab } from "./ResultsTab";

afterEach(cleanup);

const priya = { name: "Priya Nair", addr: "priya@example.com" };
const daniel = { name: "Daniel Osei", addr: "daniel@example.com" };

const chain: ChainCard = {
  chain_id: "chain-1",
  subject: "Printer lease renewal",
  first_ts: 1_700_000_000,
  last_ts: 1_700_500_000,
  message_count: 2,
  participants: { from: [priya], to: [daniel], cc: [] },
  conclusion: null,
  attachments: [],
  related: [],
  messages: [
    {
      message_key: "<m1@example.com>",
      subject: "Printer lease renewal",
      sent_ts: 1_700_000_000,
      sender: priya,
      to: [daniel],
      cc: [],
      snippet: "Invoice INV-2024-00017 for the printer lease renewal is attached for approval.",
      attachments: [],
      paths: [],
      chain_id: "chain-1",
      score: 1,
    },
  ],
  score: 1,
};

const results: SearchResponse = {
  total_messages: 1,
  total_chains: 1,
  page: 0,
  page_size: 20,
  chains: [chain],
  messages: [],
  mode: "hybrid",
};

const state: State = { ...initialState, projectId: "p1", results };

describe("ResultsTab", () => {
  it("the sort toggle dispatches setFilters with sort date", () => {
    const dispatch = vi.fn();
    render(<ResultsTab state={state} dispatch={dispatch} api={new Api()} />);

    expect(screen.getByText("1 chains · 1 emails match")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Date" }));
    expect(dispatch).toHaveBeenCalledWith({ type: "setFilters", filters: { ...initialState.filters, sort: "date" } });
  });

  it("ticking a chain card's checkbox dispatches togglePinChain", () => {
    const dispatch = vi.fn();
    render(<ResultsTab state={state} dispatch={dispatch} api={new Api()} />);

    fireEvent.click(screen.getByRole("checkbox", { name: "Pin chain Printer lease renewal" }));
    expect(dispatch).toHaveBeenCalledWith({ type: "togglePinChain", chainId: "chain-1" });
  });
});
