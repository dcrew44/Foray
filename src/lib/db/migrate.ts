import "../load-dotenv";

import { migrate } from "drizzle-orm/node-postgres/migrator";

import { db, pool } from "./index";

/**
 * Applies the committed SQL in drizzle/ and exits. Run by `pnpm db:migrate`
 * and by the one-shot `migrate` service in the prod compose profile.
 */
async function main() {
  console.log("[migrate] applying migrations from ./drizzle");
  await migrate(db, { migrationsFolder: "./drizzle" });
  console.log("[migrate] up to date");
}

main()
  .then(() => pool.end())
  .catch(async (error) => {
    console.error("[migrate] failed:", error);
    await pool.end();
    process.exit(1);
  });
