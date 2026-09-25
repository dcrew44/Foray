"use client";

import { useRouter } from "next/navigation";

import { useLiveEvent } from "./live-provider";

/**
 * Re-renders the surrounding server page when one of these events arrives —
 * a finished search changes what Home and the board should show, and the page
 * should not need a manual reload to say so.
 */
export function RefreshOn({ kinds }: { kinds: string[] }) {
  const router = useRouter();
  useLiveEvent(kinds, () => router.refresh());
  return null;
}
