import { cleanup, fireEvent, render, screen } from "@testing-library/preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { initialState } from "../state";
import { Filters } from "./Filters";

afterEach(cleanup);

describe("Filters", () => {
  it("adds a From chip on Enter and dispatches setFilters with the lower-cased address", () => {
    const dispatch = vi.fn();
    const { rerender } = render(<Filters filters={initialState.filters} dispatch={dispatch} />);

    const input = screen.getByLabelText("Add a From address");
    fireEvent.input(input, { target: { value: "  Priya.Nair@Example.COM " } });
    fireEvent.keyDown(input, { key: "Enter" });

    const next = { ...initialState.filters, from_addrs: ["priya.nair@example.com"] };
    expect(dispatch).toHaveBeenCalledTimes(1);
    expect(dispatch).toHaveBeenCalledWith({ type: "setFilters", filters: next });

    rerender(<Filters filters={next} dispatch={dispatch} />);
    expect(screen.getByRole("button", { name: "Remove priya.nair@example.com" })).toBeTruthy();
  });

  it("toggling a kind chip adds and removes it", () => {
    const dispatch = vi.fn();
    const { rerender } = render(<Filters filters={initialState.filters} dispatch={dispatch} />);

    const pdf = screen.getByRole("button", { name: "PDF" });
    expect(pdf.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(pdf);
    const withPdf = { ...initialState.filters, attachment_kinds: ["pdf"] };
    expect(dispatch).toHaveBeenLastCalledWith({ type: "setFilters", filters: withPdf });

    rerender(<Filters filters={withPdf} dispatch={dispatch} />);
    const pressed = screen.getByRole("button", { name: "PDF" });
    expect(pressed.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(pressed);
    expect(dispatch).toHaveBeenLastCalledWith({
      type: "setFilters",
      filters: { ...initialState.filters, attachment_kinds: [] },
    });
  });
});
