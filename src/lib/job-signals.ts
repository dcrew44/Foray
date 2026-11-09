import type { components } from "@/lib/api/schema";

/**
 * A posting's legitimacy signals: reasons to look twice before applying. The backend decides
 * them and writes their words (`apps/jobs/legitimacy.py`); this only sorts them for the page.
 */
export type JobSignal = components["schemas"]["JobSignal"];

/** The kinds that decide whether applying is possible at all. Every other kind is a note. */
const BLOCKING: ReadonlySet<JobSignal["kind"]> = new Set<JobSignal["kind"]>(["closed"]);

/** Splits signals into the ones the page makes prominent and the plain notes, order kept. */
export function splitSignals(signals: readonly JobSignal[]): {
  blocking: JobSignal[];
  notes: JobSignal[];
} {
  return {
    blocking: signals.filter((signal) => BLOCKING.has(signal.kind)),
    notes: signals.filter((signal) => !BLOCKING.has(signal.kind)),
  };
}
