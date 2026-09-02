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

  it("neutralises spreadsheet formula injection but leaves plain numbers alone", () => {
    const c = [{ header: "v", value: (r: { v: string }) => r.v }];
    expect(toCsv([{ v: "=1+1" }], c)).toBe("v\r\n'=1+1");
    expect(toCsv([{ v: "@SUM(A1)" }], c)).toBe("v\r\n'@SUM(A1)");
    expect(toCsv([{ v: "-1+1" }], c)).toBe("v\r\n'-1+1");
    // A genuine negative number is not a formula and is left as-is.
    const n = [{ header: "n", value: (r: { n: number }) => r.n }];
    expect(toCsv([{ n: -5 }], n)).toBe("n\r\n-5");
  });
});
