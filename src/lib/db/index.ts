import { drizzle } from "drizzle-orm/node-postgres";
import { Pool } from "pg";

import { env } from "@/env";
import * as schema from "./schema";

/**
 * One connection pool per process.
 *
 * Next's dev server re-evaluates modules on every hot reload; without the
 * globalThis cache each reload would open another pool and Postgres would run
 * out of connections within a few edits. The worker imports the same module and
 * gets its own single pool.
 */

const globalForDb = globalThis as unknown as {
  __forayPool?: Pool;
};

const pool =
  globalForDb.__forayPool ??
  new Pool({
    connectionString: env.DATABASE_URL,
    max: env.NODE_ENV === "production" ? 10 : 5,
  });

if (env.NODE_ENV !== "production") globalForDb.__forayPool = pool;

export const db = drizzle(pool, { schema, casing: "snake_case" });

export { pool };
export * as schema from "./schema";
export type Db = typeof db;
