import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The `web` Docker target copies .next/standalone and runs server.js.
  output: "standalone",

  // Native modules cannot be bundled: they are loaded from node_modules at
  // runtime instead. @napi-rs/canvas renders PDF pages to images for the
  // resume-extraction fallback when a PDF's text layer is unreadable.
  serverExternalPackages: ["@napi-rs/canvas"],
};

export default nextConfig;
