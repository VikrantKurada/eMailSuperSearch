import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Api, ApiError, csrfToken } from "./api";
import { filtersToBody, initialState } from "./state";

function jsonResponse(body: unknown, status = 200, statusText = "OK"): Response {
  return new Response(JSON.stringify(body), { status, statusText, headers: { "Content-Type": "application/json" } });
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  document.cookie = "ess_csrf=abc";
  fetchMock = vi.fn();
  globalThis.fetch = fetchMock as unknown as typeof fetch;
});

afterEach(() => {
  document.cookie = "ess_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT";
});

describe("filtersToBody", () => {
  it("omits empty lists and nulls", () => {
    expect(filtersToBody(initialState.filters, 0)).toEqual({
      query: "",
      group_by_chain: true,
      sort: "relevance",
      page: 0,
    });
  });

  it("keeps lists that have items and values that are not null, falsy ones included", () => {
    const filters = {
      ...initialState.filters,
      query: "budget",
      from_addrs: ["a@x.com"],
      recipient_addrs: ["b@x.com", "c@x.com"],
      attachment_kinds: ["pdf"],
      date_from: 0,
      date_to: 1_700_000_000,
      has_attachments: false,
      group_by_chain: false,
      only_with_conclusion: true,
      sort: "date" as const,
    };
    expect(filtersToBody(filters, 4)).toEqual({
      query: "budget",
      from_addrs: ["a@x.com"],
      recipient_addrs: ["b@x.com", "c@x.com"],
      attachment_kinds: ["pdf"],
      date_from: 0,
      date_to: 1_700_000_000,
      has_attachments: false,
      group_by_chain: false,
      only_with_conclusion: true,
      sort: "date",
      page: 4,
    });
  });
});

describe("csrfToken", () => {
  it("reads the ess_csrf cookie among others", () => {
    document.cookie = "other=1";
    expect(csrfToken()).toBe("abc");
  });
});

describe("Api", () => {
  it("non-GET requests include X-ESS-Token from the ess_csrf cookie", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse({ accepted: true }, 202)));
    const api = new Api();
    await api.syncNow("p1");
    await api.putSettings({ log_level: "DEBUG" });
    await api.deleteProject("p1");
    expect(fetchMock).toHaveBeenCalledTimes(3);
    for (const [, init] of fetchMock.mock.calls) {
      expect(init.headers["X-ESS-Token"]).toBe("abc");
    }
    const [url, init] = fetchMock.mock.calls[1];
    expect(url).toBe("/api/settings");
    expect(init.method).toBe("PUT");
    expect(init.headers["Content-Type"]).toBe("application/json");
    expect(JSON.parse(init.body)).toEqual({ log_level: "DEBUG" });
  });

  it("GET requests carry no token and no content type", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ version: "1", degraded: [], projects: [] }));
    await new Api().status();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/status");
    expect(init.method).toBe("GET");
    expect(init.headers["X-ESS-Token"]).toBeUndefined();
    expect(init.headers["Content-Type"]).toBeUndefined();
    expect(init.body).toBeUndefined();
  });

  it("search posts the filter body with page and page_size", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ total_messages: 0 }));
    await new Api().search("p1", { ...initialState.filters, query: "q" }, 2);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/projects/p1/search");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({
      query: "q",
      group_by_chain: true,
      sort: "relevance",
      page: 2,
      page_size: 20,
    });
  });

  it("exportExcel posts the filters and both pin lists", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ path: "p", name: "n.xlsx", rows: 1, truncated: false }));
    await new Api().exportExcel("p1", initialState.filters, new Set(["k1"]), new Set(["c1", "c2"]));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/projects/p1/export/excel");
    const body = JSON.parse(init.body);
    expect(body.pinned_keys).toEqual(["k1"]);
    expect(body.pinned_chain_ids).toEqual(["c1", "c2"]);
    expect(body.filters.group_by_chain).toBe(true);
  });

  it("a non-2xx response throws ApiError with the server's error text", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ error: "no such project" }, 404, "Not Found"));
    const failure = await new Api().projectSettings("nope").catch((e: unknown) => e);
    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).status).toBe(404);
    expect((failure as ApiError).message).toBe("no such project");
  });

  it("a non-2xx response without a JSON error falls back to the status text", async () => {
    fetchMock.mockResolvedValue(new Response("<html>oops</html>", { status: 502, statusText: "Bad Gateway" }));
    const failure = await new Api().purge().catch((e: unknown) => e);
    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).status).toBe(502);
    expect((failure as ApiError).message).toBe("Bad Gateway");
  });

  it("a 204 response resolves to undefined", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(new Response(null, { status: 204 })));
    await expect(new Api().deleteProject("p1")).resolves.toBeUndefined();
  });

  it("message() encodes a key containing / and @", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));
    const api = new Api();
    await api.message("p1", "<a/b@c.com>");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/projects/p1/messages/%3Ca%2Fb%40c.com%3E");
    expect(api.attachmentUrl("p1", "a/b@c", 3)).toBe("/api/projects/p1/messages/a%2Fb%40c/attachments/3");
  });

  it("openInOutlook and reveal post to the message's open and reveal routes", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(new Response(null, { status: 204 })));
    const api = new Api();
    await api.openInOutlook("p1", "k/1");
    await api.reveal("p1", "k/1");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/projects/p1/messages/k%2F1/open");
    expect(fetchMock.mock.calls[1][0]).toBe("/api/projects/p1/messages/k%2F1/reveal");
    expect(fetchMock.mock.calls[0][1].method).toBe("POST");
    expect(fetchMock.mock.calls[1][1].headers["X-ESS-Token"]).toBe("abc");
  });

  it("the other routes use the documented methods and paths", async () => {
    const api = new Api();
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse({})));
    await api.syncStatus("p1");
    await api.savedSearches("p1");
    await api.putSavedSearches("p1", []);
    await api.pickFolder();
    await api.projects();
    await api.createProject({ name: "n", folders: ["C:\\mail"], language: "en" });
    await api.project("p1");
    await api.settings();
    await api.putProjectSettings("p1", { ocr_enabled: false });
    await api.shutdown();
    expect(fetchMock.mock.calls.map((c) => `${c[1].method} ${c[0]}`)).toEqual([
      "GET /api/projects/p1/sync/status",
      "GET /api/projects/p1/saved-searches",
      "PUT /api/projects/p1/saved-searches",
      "POST /api/pick-folder",
      "GET /api/projects",
      "POST /api/projects",
      "GET /api/projects/p1",
      "GET /api/settings",
      "PUT /api/projects/p1/settings",
      "POST /api/shutdown",
    ]);
    expect(api.exportUrl("p1", "export-20260101-000000.xlsx")).toBe(
      "/api/projects/p1/exports/export-20260101-000000.xlsx",
    );
  });
});
