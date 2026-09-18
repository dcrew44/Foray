import "@/lib/load-dotenv";

import { BOARD_ADAPTERS, type BoardSourceId } from "@/lib/sources";
import { BoardNotFoundError } from "@/lib/sources/types";

/**
 * Fetch one company's board and print what the adapter made of it.
 *
 * The quickest way to see whether a slug is real and whether normalisation is
 * doing the right thing, without touching the database.
 *
 *   pnpm tsx scripts/fetch-board.ts greenhouse anthropic
 *   pnpm tsx scripts/fetch-board.ts lever matchgroup --json
 *   pnpm tsx scripts/fetch-board.ts ashby vanta --limit 3
 */

function parseArgs(argv: string[]) {
  const positional = argv.filter((arg) => !arg.startsWith("--"));
  const flag = (name: string) => argv.includes(`--${name}`);
  const value = (name: string) => {
    const index = argv.indexOf(`--${name}`);
    return index === -1 ? undefined : argv[index + 1];
  };

  return {
    source: positional[0] as BoardSourceId | undefined,
    slug: positional[1],
    json: flag("json"),
    limit: Number(value("limit") ?? 5),
  };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const sources = Object.keys(BOARD_ADAPTERS).join(" | ");

  if (!args.source || !args.slug || !(args.source in BOARD_ADAPTERS)) {
    console.error(
      `usage: pnpm tsx scripts/fetch-board.ts <${sources}> <slug> [--json] [--limit n]`,
    );
    process.exit(2);
  }

  const adapter = BOARD_ADAPTERS[args.source];
  const started = Date.now();

  let result;
  try {
    result = await adapter.fetchBoard!(args.slug);
  } catch (error) {
    if (error instanceof BoardNotFoundError) {
      console.error(`No ${args.source} board at slug "${args.slug}".`);
      process.exit(1);
    }
    throw error;
  }

  const elapsed = Date.now() - started;

  if (args.json) {
    console.log(JSON.stringify(result.jobs.slice(0, args.limit), null, 2));
    return;
  }

  console.log(
    `${args.source}:${args.slug} — ${result.jobs.length} postings in ${elapsed}ms\n`,
  );

  for (const job of result.jobs.slice(0, args.limit)) {
    const salary = job.salary.min
      ? `${job.salary.currency ?? ""}${job.salary.min.toLocaleString()}–${job.salary.max?.toLocaleString() ?? "?"}/${job.salary.period ?? "?"}`
      : "—";

    console.log(job.title);
    console.log(`  id        ${job.id}`);
    console.log(
      `  where     ${job.locations.join(" | ") || "—"}  [${job.remote}]${
        job.locationNorm?.metro ? `  metro=${job.locationNorm.metro}` : ""
      }`,
    );
    console.log(`  pay       ${salary}`);
    console.log(
      `  posted    ${job.postedAt?.toISOString().slice(0, 10) ?? "—"}${
        job.employmentType ? `   type=${job.employmentType}` : ""
      }`,
    );
    console.log(
      `  text      raw ${job.descriptionRaw?.length ?? 0} chars → cleaned ${job.descriptionText?.length ?? 0}`,
    );
    console.log(`  url       ${job.url}`);
    if (job.applyUrl) console.log(`  apply     ${job.applyUrl}`);
    console.log(`  dedupe    ${job.dedupeKey.slice(0, 12)}…\n`);
  }

  const withSalary = result.jobs.filter((job) => job.salary.min).length;
  const remote = result.jobs.filter((job) => job.remote === "remote").length;
  const bayArea = result.jobs.filter(
    (job) => job.locationNorm?.metro === "bay-area",
  ).length;

  console.log(
    `totals: ${result.jobs.length} postings · ${withSalary} with pay · ${remote} remote · ${bayArea} Bay Area`,
  );
}

void main();
