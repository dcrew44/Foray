import { headers } from "next/headers";
import { redirect } from "next/navigation";

import { auth } from "./index";

/**
 * The session, or null. Use in layouts and pages that render differently for
 * signed-out visitors.
 */
export async function getSession() {
  return auth.api.getSession({ headers: await headers() });
}

/**
 * The signed-in user, or a redirect to sign-in.
 *
 * Every page and action behind auth starts here, and every query module takes
 * the returned id as its first argument — there is no unscoped read path to
 * per-user data (DESIGN.md invariant 8).
 */
export async function requireUser() {
  const session = await getSession();
  if (!session?.user) redirect("/sign-in");
  return session.user;
}
