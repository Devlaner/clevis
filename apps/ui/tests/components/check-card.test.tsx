import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const createIssueMock = vi.fn();
const remediateMock = vi.fn();
vi.mock("@/lib/api/client", () => ({
  api: {
    issues: { create: (...a: unknown[]) => createIssueMock(...a) },
    security: { remediate: (...a: unknown[]) => remediateMock(...a) },
  },
}));

import { CheckCard } from "@/components/check-card";
import type { CheckResult } from "@/lib/api/types";

function renderWithClient(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const baseCheck: CheckResult = {
  id: "mfa",
  title: "MFA enforced",
  severity: "high",
  remediation: "Require two-factor authentication for all members.",
  status: "pass",
  value: { type: "boolean", enabled: true },
};

describe("CheckCard", () => {
  afterEach(() => {
    cleanup();
  });

  it("renders a passing check with its boolean value", () => {
    render(<CheckCard check={baseCheck} />);

    expect(screen.getByText("MFA enforced")).toBeInTheDocument();
    expect(screen.getByText("✓ Enabled")).toBeInTheDocument();
  });

  it("renders a failing check with a ratio value", () => {
    render(
      <CheckCard
        check={{
          ...baseCheck,
          status: "fail",
          value: { type: "ratio", numerator: 2, denominator: 10 },
        }}
      />,
    );

    expect(screen.getByText(/2 \/ 10/)).toBeInTheDocument();
  });

  it("renders a not_applicable check with neutral styling, not failed styling", () => {
    const { container } = render(
      <CheckCard
        check={{
          ...baseCheck,
          status: "not_applicable",
          value: null,
        }}
      />,
    );

    expect(screen.getByText("Not applicable")).toBeInTheDocument();
    expect(screen.queryByText("high")).not.toBeInTheDocument();

    const card = container.firstElementChild;
    expect(card?.className).not.toContain("border-destructive");
    expect(card?.className).not.toContain("border-accent");
  });

  it("renders non-zero severity buckets as chips for a severity_counts value", () => {
    render(
      <CheckCard
        check={{
          ...baseCheck,
          status: "fail",
          value: { type: "severity_counts", critical: 2, high: 0, medium: 1, low: 0 },
        }}
      />,
    );

    expect(screen.getByText(/2 critical/)).toBeInTheDocument();
    expect(screen.getByText(/1 medium/)).toBeInTheDocument();
    expect(screen.queryByText(/\d+ high/)).not.toBeInTheDocument();
    expect(screen.queryByText(/\d+ low/)).not.toBeInTheDocument();
  });

  it("shows a clean-state message when all severity buckets are zero", () => {
    render(
      <CheckCard
        check={{
          ...baseCheck,
          status: "pass",
          value: { type: "severity_counts", critical: 0, high: 0, medium: 0, low: 0 },
        }}
      />,
    );

    expect(screen.getByText("✓ No open alerts")).toBeInTheDocument();
  });

  it("falls back to the neutral color for a severity not present in the label map", () => {
    render(
      <CheckCard
        check={{
          ...baseCheck,
          // severity is a "high" | "medium" | "low" union at the type level,
          // but the component defends against unmapped values at runtime
          // (e.g. a future backend severity level) via `?? "text-muted-foreground"`.
          // Cast through unknown to exercise that fallback branch.
          severity: "critical" as unknown as CheckResult["severity"],
        }}
      />,
    );

    const severitySpan = screen.getByText("critical");
    expect(severitySpan.className).toContain("text-muted-foreground");
    expect(severitySpan.className).not.toContain("text-red-400");
    expect(severitySpan.className).not.toContain("text-yellow-400");
    expect(severitySpan.className).not.toContain("text-blue-400");
  });
});

describe("CheckCard — file as issue (#286)", () => {
  beforeEach(() => {
    createIssueMock.mockReset();
  });
  afterEach(() => {
    cleanup();
  });

  const failing: CheckResult = { ...baseCheck, status: "fail", value: null };

  it("shows no 'File as issue' action on a passing check or when owner is absent", () => {
    renderWithClient(<CheckCard check={baseCheck} owner="acme" />);
    expect(screen.queryByRole("button", { name: "File as issue" })).not.toBeInTheDocument();

    cleanup();
    renderWithClient(<CheckCard check={failing} />);
    expect(screen.queryByRole("button", { name: "File as issue" })).not.toBeInTheDocument();
  });

  it("files an issue with the user's edited repo and title, and links to the result", async () => {
    createIssueMock.mockResolvedValue({ number: 7, html_url: "https://github.com/acme/api/issues/7" });
    renderWithClient(<CheckCard check={failing} owner="acme" token="ghp_x" />);

    fireEvent.click(screen.getByRole("button", { name: "File as issue" }));
    expect((screen.getByLabelText("Repository") as HTMLInputElement).value).toBe(".github");
    expect((screen.getByLabelText("Issue title") as HTMLInputElement).value).toBe("MFA enforced");

    fireEvent.change(screen.getByLabelText("Repository"), { target: { value: "api" } });
    fireEvent.change(screen.getByLabelText("Issue title"), { target: { value: "Turn on org MFA" } });
    fireEvent.click(screen.getByRole("button", { name: "Create issue" }));

    await waitFor(() => expect(screen.getByText(/Issue #7 created/)).toBeInTheDocument());
    const [owner, repo, body, token] = createIssueMock.mock.calls[0];
    expect(owner).toBe("acme");
    expect(repo).toBe("api");
    expect(body.title).toBe("Turn on org MFA");
    expect(body.body).toContain("Require two-factor authentication");
    expect(token).toBe("ghp_x");
  });

  it("closes the form on Cancel without calling the API", () => {
    renderWithClient(<CheckCard check={failing} owner="acme" />);
    fireEvent.click(screen.getByRole("button", { name: "File as issue" }));
    expect(screen.getByLabelText("Repository")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByLabelText("Repository")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "File as issue" })).toBeInTheDocument();
    expect(createIssueMock).not.toHaveBeenCalled();
  });

  it("shows a scope hint when GitHub rejects the write with a 403", async () => {
    createIssueMock.mockRejectedValue(new Error("GitHub API error: 403"));
    renderWithClient(<CheckCard check={failing} owner="acme" />);

    fireEvent.click(screen.getByRole("button", { name: "File as issue" }));
    fireEvent.click(screen.getByRole("button", { name: "Create issue" }));

    await waitFor(() =>
      expect(screen.getByText(/needs the 'Issues: write' permission/)).toBeInTheDocument(),
    );
  });

  it("shows the raw error message for a non-403 failure, and a generic message otherwise", async () => {
    createIssueMock.mockRejectedValue(new Error("GitHub API error: 422"));
    renderWithClient(<CheckCard check={failing} owner="acme" />);
    fireEvent.click(screen.getByRole("button", { name: "File as issue" }));
    fireEvent.click(screen.getByRole("button", { name: "Create issue" }));
    await waitFor(() => expect(screen.getByText("GitHub API error: 422")).toBeInTheDocument());

    cleanup();
    createIssueMock.mockReset();
    createIssueMock.mockRejectedValue("plain string, not an Error");
    renderWithClient(<CheckCard check={failing} owner="acme" />);
    fireEvent.click(screen.getByRole("button", { name: "File as issue" }));
    fireEvent.click(screen.getByRole("button", { name: "Create issue" }));
    await waitFor(() => expect(screen.getByText("Failed to create the issue.")).toBeInTheDocument());
  });
});

describe("CheckCard — fix this (#287)", () => {
  beforeEach(() => {
    remediateMock.mockReset();
  });
  afterEach(() => {
    cleanup();
  });

  const remediable: CheckResult = {
    ...baseCheck,
    id: "repository_secret_scanning_enabled",
    status: "fail",
    value: null,
  };

  it("only offers 'Fix this' for a failing, auto-remediable check with an owner", () => {
    renderWithClient(<CheckCard check={{ ...remediable, status: "pass" }} owner="acme" />);
    expect(screen.queryByRole("button", { name: /fix this/i })).not.toBeInTheDocument();

    cleanup();
    renderWithClient(<CheckCard check={{ ...baseCheck, id: "mfa", status: "fail" }} owner="acme" />);
    expect(screen.queryByRole("button", { name: /fix this/i })).not.toBeInTheDocument();

    cleanup();
    renderWithClient(<CheckCard check={remediable} owner="acme" />);
    expect(screen.getByRole("button", { name: /fix this/i })).toBeInTheDocument();
  });

  it("requires a confirm click, then applies the fix and asks the page to re-scan", async () => {
    remediateMock.mockResolvedValue({ check_id: remediable.id, repo: "api", remediated: true });
    const onRemediated = vi.fn();
    renderWithClient(<CheckCard check={remediable} owner="acme" token="ghp_x" onRemediated={onRemediated} />);

    fireEvent.change(screen.getByLabelText("Repository to fix"), { target: { value: "api" } });
    fireEvent.click(screen.getByRole("button", { name: /fix this/i }));
    expect(remediateMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /confirm — apply the fix/i }));
    await waitFor(() => expect(remediateMock).toHaveBeenCalledWith("acme", "api", remediable.id, "ghp_x"));
    await waitFor(() => expect(screen.getByText(/Applied — re-run the scan/)).toBeInTheDocument());
    expect(onRemediated).toHaveBeenCalled();
  });

  it("disarms on Cancel and surfaces a failed fix", async () => {
    renderWithClient(<CheckCard check={remediable} owner="acme" />);
    fireEvent.change(screen.getByLabelText("Repository to fix"), { target: { value: "api" } });
    fireEvent.click(screen.getByRole("button", { name: /fix this/i }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: /fix this/i })).toBeInTheDocument();

    remediateMock.mockRejectedValue(new Error("GitHub rejected the change (403)."));
    fireEvent.click(screen.getByRole("button", { name: /fix this/i }));
    fireEvent.click(screen.getByRole("button", { name: /confirm/i }));
    await waitFor(() => expect(screen.getByText("GitHub rejected the change (403).")).toBeInTheDocument());
  });
});
