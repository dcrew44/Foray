import "@/lib/load-dotenv";

import { readFileSync } from "node:fs";

import { findBoard, slugCandidates, verifyBoard } from "@/lib/companies/verify";
import type { BoardSourceId } from "@/lib/sources";

/**
 * Check that board slugs are real, without touching the database.
 *
 * Used to build and re-check the seed list: a slug that 404s is worse than a
 * missing company, because it makes every later refresh waste a request and
 * look like a failure.
 *
 *   pnpm tsx scripts/verify-boards.ts --source greenhouse --slugs stripe,figma
 *   pnpm tsx scripts/verify-boards.ts --guess "Stripe" "Figma" "Notion"
 *   pnpm tsx scripts/verify-boards.ts --file candidates.json   # [{name, source?, slug?}]
 */

const CONCURRENCY = 6;

async function pooled<T, R>(
  items: T[],
  worker: (item: T) => Promise<R>,
  limit = CONCURRENCY,
): Promise<R[]> {
  const results: R[] = new Array(items.length);
  let cursor = 0;

  await Promise.all(
    Array.from({ length: Math.min(limit, items.length) }, async () => {
      for (;;) {
        const index = cursor++;
        if (index >= items.length) return;
        results[index] = await worker(items[index]);
      }
    }),
  );

  return results;
}

function flagValue(name: string): string | undefined {
  const index = process.argv.indexOf(`--${name}`);
  return index === -1 ? undefined : process.argv[index + 1];
}

async function main() {
  const argv = process.argv.slice(2);
  const source = flagValue("source") as BoardSourceId | undefined;
  const slugs = flagValue("slugs")?.split(",").map((s) => s.trim());
  const file = flagValue("file");
  const guessIndex = argv.indexOf("--guess");

  // Explicit source + slugs: just check them.
  if (source && slugs) {
    const outcomes = await pooled(slugs, (slug) => verifyBoard(source, slug));
    for (const outcome of outcomes) {
      console.log(
        `${outcome.status.padEnd(10)} ${outcome.source}:${outcome.slug}` +
          ("jobCount" in outcome ? `  ${outcome.jobCount} jobs` : "") +
          ("error" in outcome ? `  ${outcome.error}` : ""),
      );
    }
    return;
  }

  // A list of company names: guess slugs and try every provider.
  const names =
    guessIndex !== -1
      ? argv.slice(guessIndex + 1).filter((arg) => !arg.startsWith("--"))
      : file
        ? (JSON.parse(readFileSync(file, "utf8")) as Array<{ name: string }>).map(
            (entry) => entry.name,
          )
        : [];

  if (names.length === 0) {
    console.error(
      "usage: verify-boards.ts --source <s> --slugs a,b | --guess Name... | --file list.json",
    );
    process.exit(2);
  }

  const found: Array<Record<string, unknown>> = [];

  const results = await pooled(names, async (name) => {
    for (const candidate of slugCandidates(name)) {
      const outcome = await findBoard(candidate);
      if (outcome?.status === "ok") return { name, outcome };
    }
    return { name, outcome: null };
  });

  for (const { name, outcome } of results) {
    if (outcome?.status === "ok") {
      console.log(
        `✓ ${name.padEnd(28)} ${outcome.source}:${outcome.slug} (${outcome.jobCount} jobs)`,
      );
      found.push({
        name,
        sourceType: outcome.source,
        atsSlug: outcome.slug,
        slug: outcome.slug,
      });
    } else {
      console.log(`✗ ${name.padEnd(28)} no board found`);
    }
  }

  console.log(`\n${found.length}/${names.length} resolved`);
  if (process.argv.includes("--json")) {
    console.log(JSON.stringify(found, null, 2));
  }
}

void main();
