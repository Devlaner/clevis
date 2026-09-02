import { describe, expect, it } from "vitest";

import { toCsv } from "@/lib/csv";

describe("toCsv", () => {
  const cols = [
    { header: "name", value: (r: { name: string; note: string }) => r.name },
    { header: "note", value: (r: { name: string; note: string }) => r.note },
  ];

  it("emits a header row and CRLF line endings", () => {
    const csv = toCsv([{ name: "a", note: "x" }], cols);
    expect(csv).toBe("name,note\r\na,x");
  });

  it("quotes fields containing commas, quotes, or newlines and doubles embedded quotes", () => {
    const csv = toCsv([{ name: 'has, comma', note: 'say "hi"\nline' }], cols);
    expect(csv).toBe('name,note\r\n"has, comma","say ""hi""\nline"');
  });

  it("renders null/undefined as empty and stringifies numbers", () => {
    const numCols = [
      { header: "n", value: (r: { n: number | null }) => r.n },
    ];
    expect(toCsv([{ n: 0 }, { n: null }], numCols)).toBe("n\r\n0\r\n");
  });
});
