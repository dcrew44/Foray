import { defineConfig } from "drizzle-kit";

import "./src/lib/load-dotenv";

import { env } from "./src/env";

export default defineConfig({
  schema: "./src/lib/db/schema/index.ts",
  out: "./drizzle",
  dialect: "postgresql",
  casing: "snake_case",
  dbCredentials: { url: env.DATABASE_URL },
  strict: true,
  verbose: true,
});
