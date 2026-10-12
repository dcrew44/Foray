import { redirect } from "next/navigation";

import { getMeOrNull } from "@/lib/api/server";

/** The root is a doorway: home for a signed-in user, sign-in for everyone else. */
export default async function Root() {
  redirect((await getMeOrNull()) ? "/home" : "/sign-in");
}
