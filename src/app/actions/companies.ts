"use server";

import { revalidatePath } from "next/cache";

import { requireUser } from "@/lib/auth/session";
import { discoverCompany } from "@/lib/companies/discovery";
import { refreshCompany } from "@/lib/companies/refresh";
import { getCompanyBySlug, setCompanyStatus } from "@/lib/db/queries/companies";
import { QUEUES } from "@/lib/queue/queues";
import { send } from "@/lib/queue/send";

/**
 * The /companies page's actions.
 *
 * The registry is shared rather than per-user, so these require a signed-in
 * user but do not scope by one — one person adding Stripe means everyone's
 * searches cover Stripe.
 */

export async function addCompanyAction(nameOrUrl: string) {
  const user = await requireUser();

  const result = await discoverCompany({
    nameOrUrl,
    addedByUserId: user.id,
    discoveredVia: "user",
  });

  revalidatePath("/companies");

  return result.registered
    ? {
        ok: true as const,
        message: `Registered ${result.registered.name} (${result.registered.sourceType}${result.registered.atsSlug ? `:${result.registered.atsSlug}` : ""}).`,
      }
    : {
        ok: false as const,
        message: `No job board found for "${nameOrUrl}".`,
        trail: result.trail,
      };
}

export async function setStatusAction(
  slug: string,
  status: "active" | "paused",
) {
  await requireUser();
  const company = await getCompanyBySlug(slug);
  if (!company) return { ok: false as const, message: "Unknown company." };

  await setCompanyStatus(company.id, status);
  revalidatePath("/companies");
  return { ok: true as const, message: `${company.name} is now ${status}.` };
}

/** Re-fetches one board now, rather than waiting for the six-hourly cron. */
export async function refreshCompanyAction(slug: string) {
  await requireUser();
  const company = await getCompanyBySlug(slug);
  if (!company) return { ok: false as const, message: "Unknown company." };

  const outcome = await refreshCompany(company);
  revalidatePath("/companies");

  return outcome.status === "ok"
    ? {
        ok: true as const,
        message: `${company.name}: ${outcome.jobs} postings (${outcome.ingest?.inserted ?? 0} new, ${outcome.ingest?.deactivated ?? 0} closed).`,
      }
    : {
        ok: false as const,
        message: `${company.name}: ${outcome.status}${outcome.error ? ` — ${outcome.error}` : ""}`,
      };
}

/** Queues a refresh of every stale board; progress lands in the feed. */
export async function refreshAllAction() {
  const user = await requireUser();
  await send(QUEUES.companyRefresh, { userId: user.id, all: false });
  return { ok: true as const, message: "Refreshing stale boards in the background." };
}
