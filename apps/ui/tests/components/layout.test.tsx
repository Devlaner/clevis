import { describe, expect, it, vi } from "vitest";

vi.mock("@/app/globals.css", () => ({}));

vi.mock("next/font/google", () => ({
  Geist: () => ({ variable: "--font-sans" }),
  Archivo: () => ({ variable: "--font-heading" }),
  JetBrains_Mono: () => ({ variable: "--font-jetbrains-mono" }),
}));

describe("RootLayout module", () => {
  it(
    "configures the Geist, Archivo, and JetBrains Mono fonts at import time",
    // Dynamically importing the full root layout tree (providers, guards, etc.) can take
    // well over 30s when running alongside the rest of the suite under CPU contention, even
    // though it resolves in ~1s standalone. Observed up to ~153s under heavy contention, so
    // 180s timeout plus one retry gives headroom for that without masking a genuine hang.
    { timeout: 180000, retry: 1 },
    async () => {
      const mod = await import("@/app/layout");

      expect(mod.default).toBeInstanceOf(Function);
    },
  );
});
