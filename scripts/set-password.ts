import "@/lib/load-dotenv";

import { eq } from "drizzle-orm";

import { auth } from "@/lib/auth";
import { db, pool } from "@/lib/db";
import { user } from "@/lib/db/schema";

/**
 * Sets a new password for a local account, for when it has been forgotten.
 *
 *   pnpm auth:set-password you@example.com
 *
 * There is no mail transport yet, so the usual emailed reset link cannot work.
 * The password is typed at a hidden prompt, never passed as an argument, so it
 * stays out of shell history. Hashing goes through Better Auth's own context,
 * the same path its reset-password route takes, so sign-in accepts the result.
 * Existing sessions are kept.
 */

async function main() {
  const email = process.argv[2];
  if (!email) throw new Error("Usage: pnpm auth:set-password <email>");

  const [target] = await db.select().from(user).where(eq(user.email, email)).limit(1);
  if (!target) throw new Error(`No user with email ${email}.`);

  const ctx = await auth.$context;
  const accounts = await ctx.internalAdapter.findAccounts(target.id);
  if (!accounts.some((a) => a.providerId === "credential")) {
    throw new Error(`${email} does not sign in with a password.`);
  }

  const { minPasswordLength, maxPasswordLength } = ctx.password.config;

  const password = await promptHidden(`New password for ${email}: `);
  if (password.length < minPasswordLength || password.length > maxPasswordLength) {
    throw new Error(
      `Passwords must be ${minPasswordLength} to ${maxPasswordLength} characters.`,
    );
  }
  if ((await promptHidden("Type it again: ")) !== password) {
    throw new Error("Those did not match. Nothing was changed.");
  }

  await ctx.internalAdapter.updatePassword(target.id, await ctx.password.hash(password));
  console.log(`Password updated. Sign in at ${ctx.baseURL.replace(/\/api\/auth$/, "")}/sign-in`);
}

/** Reads one line from the terminal without echoing it. */
function promptHidden(question: string): Promise<string> {
  const { stdin, stdout } = process;
  if (!stdin.isTTY) {
    return Promise.reject(
      new Error("Run this in a terminal: the password is typed at a hidden prompt."),
    );
  }

  return new Promise((resolve, reject) => {
    let value = "";
    const finish = (settle: () => void) => {
      stdin.off("data", onData);
      stdin.setRawMode(false);
      stdin.pause();
      stdout.write("\n");
      settle();
    };
    const onData = (chunk: string) => {
      for (const char of chunk) {
        if (char === "\r" || char === "\n") return finish(() => resolve(value));
        if (char === "\u0003") return finish(() => reject(new Error("Cancelled.")));
        if (char === "\u007f" || char === "\b") value = value.slice(0, -1);
        else value += char;
      }
    };

    stdout.write(question);
    stdin.setRawMode(true);
    stdin.setEncoding("utf8");
    stdin.on("data", onData);
    stdin.resume();
  });
}

main()
  .then(() => pool.end())
  .catch(async (error) => {
    console.error(error instanceof Error ? error.message : error);
    await pool.end();
    process.exit(1);
  });
