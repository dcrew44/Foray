import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  test: {
    environment: "node",
    // The app's pure helpers, and the browser extension's (plain JavaScript, no build step).
    include: ["src/test/**/*.test.ts", "extension/test/unit/**/*.test.js"],
  },
});
