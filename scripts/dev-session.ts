import "../src/lib/load-dotenv";

import { randomBytes, createHmac } from "node:crypto";

import { eq } from "drizzle-orm";

import { env } from "../src/env";
import { db, pool } from "../src/lib/db";
import { session, user } from "../src/lib/db/schema";

/**
 * Mints a browser session for an existing local user, for driving the app in
 * a browser during development without typing a password anywhere.
 *
 *   pnpm tsx scripts/dev-session.ts [email]
 *
 * Prints a cookie to set on http://localhost:3000 (for example from the
 * browser console). Refuses to run in production: this is a development
 * convenience, not a way in.
 */

async function main() {
  if (env.NODE_ENV === "production") {
    throw new Error("dev-session is for local development only.");
  }
  if (!env.BETTER_AUTH_SECRET) throw new Error("BETTER_AUTH_SECRET is not set.");

  const email = process.argv[2];
  const [target] = email
    ? await db.select().from(user).where(eq(user.email, email)).limit(1)
    : await db.select().from(user).limit(1);
  if (!target) throw new Error(email ? `No user with email ${email}.` : "No users yet.");

  const token = randomBytes(24).toString("base64url");
  await db.insert(session).values({
    id: randomBytes(16).toString("hex"),
    token,
    userId: target.id,
    expiresAt: new Date(Date.now() + 7 * 24 * 60 * 60 * 1000),
    userAgent: "dev-session script",
  });

  // Better Auth signs the session cookie as `${token}.${base64(hmac)}`.
  const signature = createHmac("sha256", env.BETTER_AUTH_SECRET).update(token).digest("base64");
  const value = encodeURIComponent(`${token}.${signature}`);

  console.log(`user: ${target.email}`);
  console.log(`cookie: better-auth.session_token=${value}`);
}

main()
  .then(() => pool.end())
  .catch(async (error) => {
    console.error(error instanceof Error ? error.message : error);
    await pool.end();
    process.exit(1);
  });
