/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  // Avoid Next.js workspace-root inference issues when multiple lockfiles exist.
  // This keeps file tracing scoped to the monorepo root.
  outputFileTracingRoot: new URL("..", import.meta.url).pathname,
};

export default nextConfig;
