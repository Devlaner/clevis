import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { PageHeader } from "@/components/page-header";

describe("PageHeader", () => {
  it("renders title and description", () => {
    render(
      <PageHeader title="Repositories" description="Manage GitHub repos." />,
    );

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "Repositories",
    );
    expect(screen.getByText("Manage GitHub repos.")).toBeInTheDocument();
  });
});
