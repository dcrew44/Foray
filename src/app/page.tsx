import { redirect } from "next/navigation";

import { getSession } from "@/lib/auth/session";

/** The root is a doorway: home for a signed-in user, sign-in for everyone else. */
export default async function Root() {
  redirect((await getSession()) ? "/home" : "/sign-in");
}
