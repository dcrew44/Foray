import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The `web` Docker target copies .next/standalone and runs server.js.
  output: "standalone",
};

export default nextConfig;
