import { cleanup, fireEvent, render, screen } from "@testing-library/preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Api } from "../api";
import type { AttachmentInfo, MessageCard as Message } from "../types";
import { MessageCard } from "./MessageCard";

afterEach(cleanup);

const logo: AttachmentInfo = {
  ordinal: 0,
  filename: "image001.png",
  kind: "image",
  size: 20_000,
  extract_status: "skipped",
  ocr_status: "none",
  embedded_partial: false,
  page_count: 0,
};

const message: Message = {
  message_key: "<logo-1@corp.example>",
  subject: "Quarterly numbers",
  sent_ts: 1_791_194_400,
  sender: { name: "Alice", addr: "alice@corp.example" },
  to: [{ name: "", addr: "owner@corp.example" }],
  cc: [],
  snippet: "Hi, the quarterly numbers look fine.",
  attachments: [logo],
  paths: ["C:\\Mail\\a.eml"],
  chain_id: null,
  score: 1,
};

describe("MessageCard", () => {
  it("shows no attachment for a skipped inline image", () => {
    render(<MessageCard message={message} projectId="p1" api={new Api()} dispatch={vi.fn()} pinned={false} />);
    expect(screen.queryByText(/image001\.png/)).toBeNull();
    expect(screen.queryByRole("list")).toBeNull();
  });

  it("Reveal in Explorer calls the reveal route for the message", () => {
    const api = new Api();
    const reveal = vi.spyOn(api, "reveal").mockResolvedValue(undefined);
    render(<MessageCard message={message} projectId="p1" api={api} dispatch={vi.fn()} pinned={false} />);
    fireEvent.click(screen.getByRole("button", { name: "Reveal in Explorer" }));
    expect(reveal).toHaveBeenCalledWith("p1", "<logo-1@corp.example>");
  });

  it("shows a real attachment next to it", () => {
    const report = { ...logo, ordinal: 1, filename: "report.pdf", kind: "pdf", extract_status: "ok" };
    const both = { ...message, attachments: [logo, report] };
    render(<MessageCard message={both} projectId="p1" api={new Api()} dispatch={vi.fn()} pinned={false} />);
    expect(screen.getByText(/report\.pdf/)).toBeTruthy();
    expect(screen.queryByText(/image001\.png/)).toBeNull();
  });
});
