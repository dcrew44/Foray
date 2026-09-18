import "@/lib/load-dotenv";

import { readFileSync } from "node:fs";

import { pool } from "@/lib/db";
import { countByStatus, upsertCompany } from "@/lib/db/queries/companies";
import { refreshCompanies } from "@/lib/companies/refresh";
import { listCompanies } from "@/lib/db/queries/companies";

/**
 * Loads the seed company list into the registry.
 *
 * Idempotent: re-running updates the rows it already created rather than
 * duplicating them, so the seed file can grow over time.
 *
 *   pnpm seed:companies             # register the companies
 *   pnpm seed:companies --fetch     # and pull their boards into the mirror
 */

type SeedEntry = {
  name: string;
  slug: string;
  sourceType: "greenhouse" | "lever" | "ashby";
  atsSlug: string;
  domain?: string | null;
  tags?: string[];
};

async function main() {
  const entries = JSON.parse(
    readFileSync("src/lib/companies/seed.json", "utf8"),
  ) as SeedEntry[];

  console.log(`seeding ${entries.length} companies…`);

  let registered = 0;
  for (const entry of entries) {
    await upsertCompany({
      name: entry.name,
      slug: entry.slug,
      domain: entry.domain ?? null,
      sourceType: entry.sourceType,
      atsSlug: entry.atsSlug,
      careersUrls: [],
      tags: entry.tags ?? [],
      status: "active",
      discoveredVia: "seed",
    });
    registered += 1;
  }

  console.log(`registered ${registered}`);
  for (const row of await countByStatus()) {
    console.log(`  ${row.status}: ${row.count}`);
  }

  if (process.argv.includes("--fetch")) {
    const active = await listCompanies({ status: "active" });
    console.log(`\nfetching ${active.length} boards…`);

    const started = Date.now();
    const outcomes = await refreshCompanies(active);

    const ok = outcomes.filter((o) => o.status === "ok");
    const gone = outcomes.filter((o) => o.status === "not-found");
    const failed = outcomes.filter((o) => o.status === "error");

    const totals = ok.reduce(
      (acc, o) => ({
        jobs: acc.jobs + o.jobs,
        inserted: acc.inserted + (o.ingest?.inserted ?? 0),
        updated: acc.updated + (o.ingest?.updated ?? 0),
      }),
      { jobs: 0, inserted: 0, updated: 0 },
    );

    console.log(
      `\n${ok.length} boards ok · ${gone.length} gone · ${failed.length} errored ` +
        `in ${((Date.now() - started) / 1000).toFixed(1)}s`,
    );
    console.log(
      `${totals.jobs} postings seen → ${totals.inserted} inserted, ${totals.updated} updated`,
    );

    for (const outcome of [...gone, ...failed].slice(0, 10)) {
      console.log(`  ${outcome.status}: ${outcome.company} ${outcome.error ?? ""}`);
    }
  }

  await pool.end();
}

void main();
