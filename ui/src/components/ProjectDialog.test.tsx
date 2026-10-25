import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Api } from "../api";
import { ProjectDialog } from "./ProjectDialog";

afterEach(cleanup);

describe("ProjectDialog", () => {
  it("sends the OCR choice with the create, and no follow-up settings call", async () => {
    const api = new Api();
    const project = { id: "p1", name: "Mail", created_at: 1, dir: "C:\\data\\projects\\p1" };
    const createProject = vi.spyOn(api, "createProject").mockResolvedValue(project);
    const putProjectSettings = vi.spyOn(api, "putProjectSettings").mockResolvedValue({} as never);
    const onCreated = vi.fn();
    render(<ProjectDialog api={api} heading="New project" onCreated={onCreated} />);

    fireEvent.input(screen.getByRole("textbox", { name: "Name" }), { target: { value: "Mail" } });
    fireEvent.input(screen.getByRole("textbox", { name: "Folder path" }), { target: { value: "C:\\Mail" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "Recover text from scanned attachments (OCR)" }));
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(project, null));
    expect(createProject).toHaveBeenCalledWith({ name: "Mail", folders: ["C:\\Mail"], ocr_enabled: false });
    expect(putProjectSettings).not.toHaveBeenCalled();
  });
});
