"use client";

import { createAuthClient } from "better-auth/react";

/**
 * Same-origin, so no baseURL: the browser talks to /api/auth on whatever host
 * it loaded the app from.
 */
export const authClient = createAuthClient();

export const { signIn, signUp, signOut, useSession } = authClient;
