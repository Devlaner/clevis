import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { EmptyStateInline, EmptyStateNoAccount, EmptyStatePage } from "@/components/empty-state";

afterEach(cleanup);

describe("EmptyStateInline", () => {
  it("renders the noun with no qualifier", () => {
    render(<EmptyStateInline noun="jobs" />);

    expect(screen.getByText("No jobs")).toBeInTheDocument();
  });

  it("renders the qualifier when provided", () => {
    render(<EmptyStateInline noun="repositories" qualifier="acme" />);

    expect(screen.getByText('No repositories matching "acme"')).toBeInTheDocument();
  });
});

describe("EmptyStatePage", () => {
  it("renders the message with no action", () => {
    render(<EmptyStatePage message="No organization configured yet." />);

    expect(screen.getByText("No organization configured yet.")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("renders an action link when provided", () => {
    render(
      <EmptyStatePage
        message="No organization configured yet."
        action={{ href: "/security", label: "Configure" }}
      />,
    );

    const link = screen.getByRole("link", { name: "Configure" });
    expect(link).toHaveAttribute("href", "/security");
  });
});

describe("EmptyStateNoAccount", () => {
  it("renders the default copy when no message override is given", () => {
    render(<EmptyStateNoAccount />);
    expect(screen.getByText(/this page has nothing to query/)).toBeInTheDocument();
  });

  it("renders a custom message when one is provided", () => {
    render(<EmptyStateNoAccount message="No account selected — type an organization below." />);
    expect(screen.getByText("No account selected — type an organization below.")).toBeInTheDocument();
    expect(screen.queryByText(/this page has nothing to query/)).not.toBeInTheDocument();
  });

  it("renders a real CTA button pointing at Settings, not just a text link", () => {
    render(<EmptyStateNoAccount />);
    const link = screen.getByRole("link", { name: "Connect a GitHub account" });
    expect(link).toHaveAttribute("href", "/settings");
  });
});
