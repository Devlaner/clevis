import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MyItemsList } from "@/components/my-items-list";

const ITEM = {
  number: 12,
  title: "Fix bug",
  repository: "acme/api",
  html_url: "https://github.com/acme/api/pull/12",
  updated_at: new Date().toISOString(),
};

afterEach(() => {
  cleanup();
});

describe("MyItemsList", () => {
  it("renders items with repo/number and a working link", () => {
    render(
      <MyItemsList
        items={[ITEM]}
        isLoading={false}
        isError={false}
        errorMessage=""
        onRetry={() => {}}
        retrying={false}
        emptyNoun="pull requests"
        totalCount={1}
        page={1}
        perPage={25}
        onPageChange={() => {}}
      />,
    );
    expect(screen.getByText("Fix bug")).toBeInTheDocument();
    expect(screen.getByText("acme/api #12")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Fix bug/ })).toHaveAttribute("href", ITEM.html_url);
  });

  it("shows the empty state only when there are genuinely no items", () => {
    render(
      <MyItemsList
        items={[]}
        isLoading={false}
        isError={false}
        errorMessage=""
        onRetry={() => {}}
        retrying={false}
        emptyNoun="pull requests"
        totalCount={0}
        page={1}
        perPage={25}
        onPageChange={() => {}}
      />,
    );
    expect(screen.getByText(/No pull requests/)).toBeInTheDocument();
  });

  it("shows a retry option instead of items when isError is set", () => {
    const onRetry = vi.fn();
    render(
      <MyItemsList
        items={[]}
        isLoading={false}
        isError={true}
        errorMessage="Workspace admin access required"
        onRetry={onRetry}
        retrying={false}
        emptyNoun="pull requests"
        totalCount={0}
        page={1}
        perPage={25}
        onPageChange={() => {}}
      />,
    );
    expect(screen.getByText("Workspace admin access required")).toBeInTheDocument();
    expect(screen.queryByText(/No pull requests/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("shows a loading state and no rows while isLoading", () => {
    render(
      <MyItemsList
        items={[]}
        isLoading={true}
        isError={false}
        errorMessage=""
        onRetry={() => {}}
        retrying={false}
        emptyNoun="pull requests"
        totalCount={0}
        page={1}
        perPage={25}
        onPageChange={() => {}}
      />,
    );
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText(/No pull requests/)).not.toBeInTheDocument();
  });

  it("disables Prev on page 1 and Next on the last page, and calls onPageChange", () => {
    const onPageChange = vi.fn();
    render(
      <MyItemsList
        items={[ITEM]}
        isLoading={false}
        isError={false}
        errorMessage=""
        onRetry={() => {}}
        retrying={false}
        emptyNoun="pull requests"
        totalCount={30}
        page={1}
        perPage={25}
        onPageChange={onPageChange}
      />,
    );
    expect(screen.getByRole("button", { name: "Prev" })).toBeDisabled();
    const nextButton = screen.getByRole("button", { name: "Next" });
    expect(nextButton).not.toBeDisabled();
    fireEvent.click(nextButton);
    expect(onPageChange).toHaveBeenCalledWith(2);
  });

  it("disables Next once page*perPage reaches total_count", () => {
    render(
      <MyItemsList
        items={[ITEM]}
        isLoading={false}
        isError={false}
        errorMessage=""
        onRetry={() => {}}
        retrying={false}
        emptyNoun="pull requests"
        totalCount={30}
        page={2}
        perPage={25}
        onPageChange={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Prev" })).not.toBeDisabled();
  });

  it("calls onPageChange with the previous page when Prev is clicked", () => {
    const onPageChange = vi.fn();
    render(
      <MyItemsList
        items={[ITEM]}
        isLoading={false}
        isError={false}
        errorMessage=""
        onRetry={() => {}}
        retrying={false}
        emptyNoun="pull requests"
        totalCount={30}
        page={2}
        perPage={25}
        onPageChange={onPageChange}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Prev" }));
    expect(onPageChange).toHaveBeenCalledWith(1);
  });

  it("shows a distinct message (not the generic empty state) when identityUnresolved is set", () => {
    render(
      <MyItemsList
        items={[]}
        isLoading={false}
        isError={false}
        errorMessage=""
        onRetry={() => {}}
        retrying={false}
        emptyNoun="pull requests"
        totalCount={0}
        page={1}
        perPage={25}
        onPageChange={() => {}}
        identityUnresolved={true}
      />,
    );
    expect(screen.getByText(/can’t tell who you are on GitHub/)).toBeInTheDocument();
    expect(screen.queryByText(/No pull requests/)).not.toBeInTheDocument();
  });

  it("shows a retrying spinner state on the retry button", () => {
    render(
      <MyItemsList
        items={[]}
        isLoading={false}
        isError={true}
        errorMessage="boom"
        onRetry={() => {}}
        retrying={true}
        emptyNoun="pull requests"
        totalCount={0}
        page={1}
        perPage={25}
        onPageChange={() => {}}
      />,
    );
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    expect(screen.getByRole("button")).toBeDisabled();
  });
});
