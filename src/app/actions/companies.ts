"use server";

import { and, count, eq } from "drizzle-orm";
import { revalidatePath } from "next/cache";

import { BOARD_LABEL, FETCH_PROBLEM, toCompanyRow, type CompanyRow } from "@/components/companies/company-row";
import { requireUser } from "@/lib/auth/session";
import { discoverCompany, type DiscoveryStep } from "@/lib/companies/discovery";
import { refreshCompany, staleCutoff } from "@/lib/companies/refresh";
import { db } from "@/lib/db";
import { getCompanyBySlug, setCompanyStatus, staleCompanies } from "@/lib/db/queries/companies";
import { jobs, type Company } from "@/lib/db/schema";
import { plural } from "@/lib/format";
import { QUEUES } from "@/lib/queue/queues";
import { send } from "@/lib/queue/send";

/**
 * The /companies page's actions.
 *
 * The registry is shared rather than per-user, so these require a signed-in
 * user but do not scope by one — one person adding Stripe means everyone's
 * searches cover Stripe. Each returns the company's updated row so the table
 * can show the result without waiting for the page to re-render.
 */

type Failure = { ok: false; message: string };

export type AddCompanyResult =
  | { ok: true; message: string; company: CompanyRow; existed: boolean }
  | (Failure & { trail: DiscoveryStep[] });

export async function addCompanyAction(nameOrUrl: string): Promise<AddCompanyResult> {
  const user = await requireUser();

  const query = nameOrUrl.trim();
  if (!query) {
    return { ok: false, message: "Type a company name, its website or a job board link.", trail: [] };
  }
  if (query.length > 300) {
    return { ok: false, message: "That is too long to be a company name or a link.", trail: [] };
  }

  const startedAt = Date.now();
  let result: Awaited<ReturnType<typeof discoverCompany>>;
  try {
    result = await discoverCompany({
      nameOrUrl: query,
      addedByUserId: user.id,
      discoveredVia: "user",
    });
  } catch (error) {
    return {
      ok: false,
      message: `Looking for “${query}” stopped with an error: ${error instanceof Error ? error.message : String(error)}`,
      trail: [],
    };
  }

  if (!result.registered) {
    const looksLikeUrl = /^(?:https?:\/\/|www\.)|\.[a-z]{2,}(?:\/|$)/i.test(query);
    return {
      ok: false,
      message: looksLikeUrl
        ? `No job board found at “${query}”. Try a direct link to its jobs page, or the company's name.`
        : `No job board found for “${query}”. Try its website or a link to its jobs page.`,
      trail: result.trail,
    };
  }

  let company = result.registered;
  const existed = company.createdAt.getTime() < startedAt;

  // Fill the board straight away so the company is useful now, not after the
  // next scheduled refresh. ATS boards are free public APIs; a careers page
  // needs a model to read it, so that waits for the scheduled refresh.
  const hasApi = company.sourceType !== "careers_page" && company.sourceType !== "none";
  if (hasApi && company.status === "active") {
    await refreshCompany(company);
    company = (await getCompanyBySlug(company.slug)) ?? company;
  }

  const row = toCompanyRow(company, await activeJobCount(company.id));
  revalidatePath("/companies");

  const detail = hasApi
    ? `Its ${BOARD_LABEL[company.sourceType]} board has ${plural(row.activeJobs, "open role")}.`
    : "Its careers page is read at the next scheduled refresh.";

  return {
    ok: true,
    existed,
    company: row,
    message: existed
      ? `${company.name} was already on the list. ${detail}`
      : `Added ${company.name}. ${detail}`,
  };
}

export async function setStatusAction(
  slug: string,
  status: "active" | "paused",
): Promise<{ ok: true; message: string; company: CompanyRow } | Failure> {
  await requireUser();
  const company = await getCompanyBySlug(slug);
  if (!company) return { ok: false, message: "That company is no longer on the list." };

  const updated = (await setCompanyStatus(company.id, status)) ?? company;
  revalidatePath("/companies");
  return {
    ok: true,
    company: toCompanyRow(updated, await activeJobCount(company.id)),
    message:
      status === "paused"
        ? `Paused ${company.name}. Its board is skipped until you resume it.`
        : `${company.name} is active again and will be fetched with the rest.`,
  };
}

/** Re-fetches one board now, rather than waiting for the six-hourly refresh. */
export async function refreshCompanyAction(
  slug: string,
): Promise<{ ok: true; message: string; company: CompanyRow } | (Failure & { company?: CompanyRow })> {
  await requireUser();
  const company = await getCompanyBySlug(slug);
  if (!company) return { ok: false, message: "That company is no longer on the list." };

  const outcome = await refreshCompany(company);
  const updated = (await getCompanyBySlug(slug)) ?? company;
  const row = toCompanyRow(updated, await activeJobCount(company.id));
  revalidatePath("/companies");

  if (outcome.status === "ok") {
    const inserted = outcome.ingest?.inserted ?? 0;
    const closed = outcome.ingest?.deactivated ?? 0;
    return {
      ok: true,
      company: row,
      message: `${company.name}: ${plural(outcome.jobs, "open role")}${
        inserted || closed ? `, ${inserted} new and ${closed} closed since the last fetch` : ", no changes"
      }.`,
    };
  }

  return {
    ok: false,
    company: row,
    message: `${company.name}: ${(FETCH_PROBLEM[outcome.status] ?? "the fetch failed").toLowerCase()}${
      outcome.status === "not-found" ? ", so it is marked invalid" : ""
    }.${outcome.error && outcome.status === "error" ? ` ${outcome.error}` : ""}`,
  };
}

/** Queues a refresh of every stale board; the result lands in the activity feed. */
export async function refreshAllAction(): Promise<
  { ok: true; message: string; queued: number } | Failure
> {
  const user = await requireUser();

  const stale = await staleCompanies(staleCutoff());
  if (stale.length === 0) {
    return {
      ok: true,
      queued: 0,
      message: "Every active board was fetched in the last two hours, so none are stale.",
    };
  }

  try {
    await send(QUEUES.companyRefresh, { userId: user.id, all: false });
  } catch {
    return { ok: false, message: "The refresh could not be queued. Check that the database is running." };
  }
  return {
    ok: true,
    queued: stale.length,
    message: `Refreshing ${plural(stale.length, "stale board")} in the background. The table updates when they are done.`,
  };
}

async function activeJobCount(companyId: Company["id"]): Promise<number> {
  const [row] = await db
    .select({ n: count(jobs.id) })
    .from(jobs)
    .where(and(eq(jobs.companyId, companyId), eq(jobs.isActive, true)));
  return row?.n ?? 0;
}
