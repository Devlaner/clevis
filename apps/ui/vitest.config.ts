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
    include: ["../../tests/ui/**/*.{test,spec}.{ts,tsx}"],
    passWithNoTests: false,
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
