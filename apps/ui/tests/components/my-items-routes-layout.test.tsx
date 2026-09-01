import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const redirect = vi.fn();
vi.mock("next/navigation", () => ({ redirect: (...a: unknown[]) => redirect(...a) }));

import MyWorkLayout, { metadata as myMetadata } from "@/app/my/layout";
import ReleasesLayout, { metadata as releasesMetadata } from "@/app/releases/layout";
import MyPRsRedirect from "@/app/my/prs/page";
import MyReviewsRedirect from "@/app/my/reviews/page";
import MyIssuesRedirect from "@/app/my/issues/page";

describe("My Work + Releases route layouts", () => {
  afterEach(() => {
    cleanup();
    redirect.mockReset();
  });

  it.each([
    ["My Work", MyWorkLayout, myMetadata, "My Work · clevis"],
    ["Releases", ReleasesLayout, releasesMetadata, "Releases · clevis"],
  ])("%s layout sets its page title and renders children", (_label, Layout, metadata, expectedTitle) => {
    expect(metadata.title).toBe(expectedTitle);

    render(
      <Layout>
        <p>child content</p>
      </Layout>,
    );

    expect(screen.getByText("child content")).toBeInTheDocument();
  });

  it.each([
    ["/my/prs", MyPRsRedirect, "/my"],
    ["/my/reviews", MyReviewsRedirect, "/my?tab=reviews"],
    ["/my/issues", MyIssuesRedirect, "/my?tab=issues"],
  ])("%s still redirects to the merged tabbed page (issue #283)", (_label, Page, target) => {
    Page();
    expect(redirect).toHaveBeenCalledWith(target);
  });
});
