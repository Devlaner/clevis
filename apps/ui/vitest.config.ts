import path from "node:path";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

const uiRoot = path.resolve(__dirname);
const repoRoot = path.resolve(__dirname, "../..");

export default defineConfig({
  root: uiRoot,
  server: {
    fs: {
      allow: [repoRoot],
    },
  },
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    include: ["./tests/**/*.{test,spec}.{ts,tsx}"],
    passWithNoTests: false,
    // Default (5000ms) is too tight once all ~43 files run concurrently -- CPU contention
    // between jsdom environments intermittently pushes individual tests past it (a different
    // file each run, not a real bug in any one of them). 20s gives headroom without masking a
    // genuine hang; layout.test.tsx keeps its own higher override for its unusually slow import.
    testTimeout: 20000,
    // Vitest's default is one worker fork per CPU core. On a memory-constrained dev machine
    // (the pre-push hook is the common victim) ~45 concurrent jsdom + React environments
    // exhaust RAM -> the OS swaps -> module-import times blow up into the minutes -> tinypool
    // worker RPCs time out, which Vitest reports as "N errors" with zero test failures, a
    // different set every run. Capping the pool bounds peak memory and makes local runs
    // deterministic. CI runners are dedicated and adequately provisioned, so they keep full
    // parallelism for speed. Raise the local cap with VITEST_MAX_WORKERS=<n> (Vitest honors
    // that env var natively) if your machine has the headroom.
    ...(process.env.CI ? {} : { maxWorkers: 3, minWorkers: 1 }),
    coverage: {
      provider: "v8",
      reporter: ["text", "lcov", "json-summary"],
      include: ["app/**", "components/**", "lib/**", "hooks/**"],
      exclude: ["**/*.d.ts", "components/ui/**"],
      // Global floor is a regression guard, not an aspirational target — most `app/**` page
      // components have no unit tests yet (large, integration-style route components; this
      // repo's convention so far is unit-testing extracted logic/hooks/components, not full
      // pages). Measured baseline: ~24.6%/18%/15.7%/23.7%. Set a few points below so normal
      // fluctuation doesn't fail CI, while still catching a real drop. New/changed lines in a
      // PR are separately held to a much higher bar by the diff-coverage check in CI.
      thresholds: {
        statements: 22,
        branches: 16,
        functions: 13,
        lines: 21,
      },
    },
  },
  resolve: {
    dedupe: ["react", "react-dom"],
    alias: {
      "@": uiRoot,
      react: path.join(uiRoot, "node_modules/react"),
      "react-dom": path.join(uiRoot, "node_modules/react-dom"),
      "@testing-library/react": path.join(
        uiRoot,
        "node_modules/@testing-library/react",
      ),
      "@testing-library/jest-dom": path.join(
        uiRoot,
        "node_modules/@testing-library/jest-dom",
      ),
      vitest: path.join(uiRoot, "node_modules/vitest"),
    },
  },
});
