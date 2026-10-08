import type { NextConfig } from "next";

// Where Next sends /api/*: Django. Rewrites are frozen into the build's routes manifest, so the
// Docker `web` build bakes in the in-network name; native `next dev` reads DJANGO_ORIGIN from
// .env (http://127.0.0.1:8000). Server components read process.env.DJANGO_ORIGIN at runtime.
const DJANGO_ORIGIN = process.env.DJANGO_ORIGIN ?? "http://api:8000";

const nextConfig = {
  // The `web` Docker target copies .next/standalone and runs server.js.
  output: "standalone",

  // Native modules cannot be bundled: they are loaded from node_modules at
  // runtime instead. @napi-rs/canvas renders PDF pages to images for the
  // resume-extraction fallback when a PDF's text layer is unreadable.
  serverExternalPackages: ["@napi-rs/canvas"],

  experimental: {
    // The proxy cuts idle sockets at 30 s by default; a chat stream can idle longer between
    // events (it also sends `: ping` every 15 s).
    proxyTimeout: 10 * 60_000,
    // Larger bodies are truncated into a bare 500. 16 MB leaves room for the 10 MB resume limit
    // plus multipart overhead, so the user sees Django's friendly 413 instead.
    proxyClientMaxBodySize: "16mb",
  },

  // No Next route handler exists under /api (src/app/api is gone), so a plain rewrite sends
  // every /api request to Django. Django marks streams `no-transform`, so Next's compression
  // passes them through unbuffered.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${DJANGO_ORIGIN}/api/:path*` }];
  },
} satisfies NextConfig;

export default nextConfig;
