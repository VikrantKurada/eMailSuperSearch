import { cleanup, render, screen } from "@testing-library/preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Api } from "../api";
import type { Status } from "../types";
import { StatusLine } from "./StatusLine";

afterEach(cleanup);

describe("StatusLine", () => {
  it("shows the .msg handler badge with its advice", () => {
    const status: Status = { version: "0.1.0", degraded: ["msg_handler"], projects: [] };
    render(
      <StatusLine status={status} sync={null} projectId={null} busy={false} error={null} api={new Api()} dispatch={vi.fn()} />,
    );
    const badge = screen.getByText("No .msg handler");
    expect(badge.getAttribute("title")).toBe("Open in Outlook may fail; use Reveal in Explorer");
  });
});
