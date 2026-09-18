import { betterAuth } from "better-auth";
import { drizzleAdapter } from "better-auth/adapters/drizzle";
import { nextCookies } from "better-auth/next-js";

import { env } from "@/env";
import { db } from "@/lib/db";
import { account, session, user, verification } from "@/lib/db/schema";

/**
 * Google is configured only when its credentials exist, so local development
 * never blocks on a Google Cloud project. It is wired now rather than later
 * because Phase 3's Gmail scopes are added to this same account via
 * `linkSocial`, not by a second provider.
 */
const socialProviders =
  env.GOOGLE_CLIENT_ID && env.GOOGLE_CLIENT_SECRET
    ? {
        google: {
          clientId: env.GOOGLE_CLIENT_ID,
          clientSecret: env.GOOGLE_CLIENT_SECRET,
        },
      }
    : undefined;

export const auth = betterAuth({
  baseURL: env.BETTER_AUTH_URL,
  secret: env.BETTER_AUTH_SECRET,

  database: drizzleAdapter(db, {
    provider: "pg",
    schema: { user, session, account, verification },
  }),

  emailAndPassword: {
    // Enabled so a single developer can sign in without any third-party setup.
    enabled: true,
    // No mail transport in Phase 1; verification arrives with real deployment.
    requireEmailVerification: false,
  },

  socialProviders,

  // Writes the session cookie from server actions and route handlers.
  plugins: [nextCookies()],
});

export type Session = typeof auth.$Infer.Session;
