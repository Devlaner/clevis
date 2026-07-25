import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import Layout, { metadata } from "@/app/pulls/layout";

describe("Pull Requests layout", () => {
  it("renders its children through unmodified", () => {
    render(
      <Layout>
        <p>child content</p>
      </Layout>,
    );
    expect(screen.getByText("child content")).toBeInTheDocument();
  });

  it("sets the page title", () => {
    expect(metadata.title).toBe("Pull Requests · clevis");
  });
});
