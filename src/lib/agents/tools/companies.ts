import { tool } from "ai";
import { z } from "zod";

import { discoverCompany } from "@/lib/companies/discovery";
import { countByStatus, listCompanies } from "@/lib/db/queries/companies";
import { QUEUES } from "@/lib/queue/queues";
import { send } from "@/lib/queue/send";
import { generateStructured } from "@/lib/llm/structured";

/**
 * Chat's handles on the company registry.
 *
 * `add_company` runs inline because the user is waiting and it is one company.
 * `discover_companies` fans out over many, which can take minutes, so it is
 * enqueued and reports through the activity feed instead.
 */

export function addCompany(userId: string) {
  return tool({
    description:
      "Register one company's job board so its postings are searched. Accepts " +
      "a company name, a website, or a job-board URL. Verifies the board is " +
      "real before registering it.",
    inputSchema: z.object({
      name_or_url: z
        .string()
        .describe('A company name ("Stripe"), a domain, or a job-board URL.'),
    }),
    execute: async ({ name_or_url }) => {
      const result = await discoverCompany({
        nameOrUrl: name_or_url,
        addedByUserId: userId,
        discoveredVia: "user",
      });

      if (!result.registered) {
        return {
          ok: false,
          message: `No job board found for "${name_or_url}".`,
          // The trail is what makes a miss actionable — it says which guesses
          // were tried, so the user can supply the board URL directly.
          tried: result.trail,
        };
      }

      return {
        ok: true,
        company: result.registered.name,
        source: result.registered.sourceType,
        slug: result.registered.atsSlug,
        needsBrowser: result.needsBrowser,
        message: `Registered ${result.registered.name}. Its postings will be pulled in on the next refresh.`,
      };
    },
  });
}

const ProposedCompanies = z.object({
  companies: z.array(
    z.object({
      name: z.string(),
      why: z.string().describe("One short clause on why this company fits."),
    }),
  ),
});

export function discoverCompanies(userId: string) {
  return tool({
    description:
      "Find and register several companies matching a description, e.g. " +
      "'AI infrastructure startups in SF hiring new grads'. Runs in the " +
      "background; progress appears in the activity feed.",
    inputSchema: z.object({
      description: z.string(),
      count: z
        .number()
        .int()
        .min(1)
        .max(25)
        .describe("How many companies to look for. Keep it modest."),
    }),
    execute: async ({ description, count }) => {
      // The model proposes names; discovery is what decides whether any of them
      // are real. Nothing is registered on the strength of a suggestion.
      const { value } = await generateStructured(
        "chat",
        ProposedCompanies,
        {
          instructions:
            "You name real companies matching a description. Only companies " +
            "you are confident actually exist. No descriptions of what they " +
            "might be called — exact names as they are commonly written.",
          prompt: `Name up to ${count} companies matching: ${description}`,
        },
        { userId },
      );

      const names = value.companies.slice(0, count).map((c) => c.name);
      if (names.length === 0) {
        return { ok: false, message: "Could not think of matching companies." };
      }

      await send(QUEUES.companyDiscover, { userId, names });

      return {
        ok: true,
        queued: names,
        message: `Looking for job boards for ${names.length} companies: ${names.join(", ")}. Progress will appear in the activity feed.`,
      };
    },
  });
}

export function listRegisteredCompanies() {
  return tool({
    description:
      "Summarise which companies are registered and searchable, with counts " +
      "by status.",
    inputSchema: z.object({
      limit: z.number().int().min(1).max(50).nullable(),
    }),
    execute: async ({ limit }) => {
      const [counts, active] = await Promise.all([
        countByStatus(),
        listCompanies({ status: "active", limit: limit ?? 20 }),
      ]);

      return {
        counts: Object.fromEntries(counts.map((c) => [c.status, c.count])),
        sample: active.map((company) => ({
          name: company.name,
          source: company.sourceType,
          lastFetched: company.lastFetchedAt,
          jobs: company.lastFetchJobCount,
        })),
      };
    },
  });
}
