import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { DataTable, type DataTableColumn } from "@/components/ui/data-table";

interface Row {
  id: number;
  name: string;
  score: number;
}

const ROWS: Row[] = [
  { id: 1, name: "Charlie", score: 30 },
  { id: 2, name: "Alice", score: 10 },
  { id: 3, name: "Bob", score: 20 },
];

const COLUMNS: DataTableColumn<Row>[] = [
  { key: "name", header: "Name", sortValue: (r) => r.name, render: (r) => r.name },
  { key: "score", header: "Score", align: "right", sortValue: (r) => r.score, render: (r) => String(r.score) },
];

afterEach(() => {
  cleanup();
});

describe("DataTable", () => {
  it("renders rows in the given order when no sort is applied", () => {
    render(<DataTable columns={COLUMNS} data={ROWS} getRowKey={(r) => r.id} />);
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("Charlie");
    expect(rows[1]).toHaveTextContent("Alice");
    expect(rows[2]).toHaveTextContent("Bob");
  });

  it("sorts ascending on first header click and descending on the second", () => {
    render(<DataTable columns={COLUMNS} data={ROWS} getRowKey={(r) => r.id} />);

    fireEvent.click(screen.getByText("Name"));
    let rows = screen.getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("Alice");
    expect(rows[1]).toHaveTextContent("Bob");
    expect(rows[2]).toHaveTextContent("Charlie");

    fireEvent.click(screen.getByText("Name"));
    rows = screen.getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("Charlie");
    expect(rows[1]).toHaveTextContent("Bob");
    expect(rows[2]).toHaveTextContent("Alice");
  });

  it("switches the active sort column when a different header is clicked", () => {
    render(<DataTable columns={COLUMNS} data={ROWS} getRowKey={(r) => r.id} />);

    fireEvent.click(screen.getByText("Score"));
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("Alice"); // score 10
    expect(rows[1]).toHaveTextContent("Bob"); // score 20
    expect(rows[2]).toHaveTextContent("Charlie"); // score 30
  });

  it("does not attach a sort handler to columns without sortValue", () => {
    const columns: DataTableColumn<Row>[] = [{ key: "name", header: "Name", render: (r) => r.name }];
    render(<DataTable columns={columns} data={ROWS} getRowKey={(r) => r.id} />);
    const header = screen.getByText("Name").closest("th")!;
    expect(header.className).not.toContain("cursor-pointer");
  });

  it("paginates once data exceeds pageSize, and Prev/Next move between pages", () => {
    render(<DataTable columns={COLUMNS} data={ROWS} getRowKey={(r) => r.id} pageSize={2} />);

    expect(screen.getByText("Page 1 of 2 · 3 total")).toBeInTheDocument();
    let rows = screen.getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Prev" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText("Page 2 of 2 · 3 total")).toBeInTheDocument();
    rows = screen.getAllByRole("row").slice(1);
    expect(rows).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("does not show pagination controls when data fits within one page", () => {
    render(<DataTable columns={COLUMNS} data={ROWS} getRowKey={(r) => r.id} pageSize={10} />);
    expect(screen.queryByRole("button", { name: "Prev" })).not.toBeInTheDocument();
  });

  it("resets to page 1 when the data changes", () => {
    const { rerender } = render(
      <DataTable columns={COLUMNS} data={ROWS} getRowKey={(r) => r.id} pageSize={2} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText("Page 2 of 2 · 3 total")).toBeInTheDocument();

    rerender(
      <DataTable
        columns={COLUMNS}
        data={[...ROWS, { id: 4, name: "Dana", score: 40 }]}
        getRowKey={(r) => r.id}
        pageSize={2}
      />,
    );
    expect(screen.getByText("Page 1 of 2 · 4 total")).toBeInTheDocument();
  });

  it("attaches a ref to the row selected by getRowRef", () => {
    let refEl: HTMLTableRowElement | null = null;
    render(
      <DataTable
        columns={COLUMNS}
        data={ROWS}
        getRowKey={(r) => r.id}
        getRowRef={(r) => (r.id === 2 ? (el: HTMLTableRowElement | null) => { refEl = el; } : undefined)}
      />,
    );
    expect(refEl).not.toBeNull();
    expect(refEl!).toHaveTextContent("Alice");
  });
});
