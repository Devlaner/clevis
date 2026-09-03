import { afterEach, describe, expect, it, vi } from "vitest";

import { downloadTextFile } from "@/lib/download";

describe("downloadTextFile", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("creates an anchor, clicks it, and revokes the object URL", () => {
    vi.useFakeTimers();
    const createUrl = vi.fn(() => "blob:fake");
    const revokeUrl = vi.fn();
    // jsdom implements URL but not the object-URL methods.
    vi.stubGlobal("URL", { ...URL, createObjectURL: createUrl, revokeObjectURL: revokeUrl });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    downloadTextFile("report.csv", "a,b\r\n1,2", "text/csv");

    expect(createUrl).toHaveBeenCalledOnce();
    expect(click).toHaveBeenCalledOnce();
    // Anchor is cleaned up synchronously.
    expect(document.querySelector("a[download]")).toBeNull();
    vi.runAllTimers();
    expect(revokeUrl).toHaveBeenCalledWith("blob:fake");
  });
});
