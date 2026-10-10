import type { NextConfig } from "next";

// Where Next sends /api/*: Django. Rewrites are frozen into the build's routes manifest, so the
// Docker `web` build bakes in the in-network name; native `next dev` reads DJANGO_ORIGIN from
// .env (http://127.0.0.1:8000). Server components read process.env.DJANGO_ORIGIN at runtime.
const DJANGO_ORIGIN = process.env.DJANGO_ORIGIN ?? "http://api:8000";

const nextConfig = {
  // The `web` Docker target copies .next/standalone and runs server.js.
  output: "standalone",

  experimental: {
    // The proxy cuts idle sockets at 30 s by default; a chat stream can idle longer between
    // events (it also sends `: ping` every 15 s).
    proxyTimeout: 10 * 60_000,
    // 16 MB leaves room for the 10 MB resume limit plus multipart overhead, so a file a little
    // over the limit gets Django's friendly 413. A larger body is forwarded truncated under its
    // original length and hangs until proxyTimeout, so the dropzone refuses anything over 10 MB
    // itself, with the same sentence, before sending (src/lib/uploads.ts).
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
