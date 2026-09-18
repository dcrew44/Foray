import { ashby } from "./ashby";
import { greenhouse } from "./greenhouse";
import { lever } from "./lever";
import type { JobSourceAdapter } from "./types";

/** Every board adapter, by the source id stored on a job. */
export const BOARD_ADAPTERS = { greenhouse, lever, ashby } as const;

export type BoardSourceId = keyof typeof BOARD_ADAPTERS;

export function boardAdapter(source: string): JobSourceAdapter | null {
  return BOARD_ADAPTERS[source as BoardSourceId] ?? null;
}

export * from "./types";
export { ashby, greenhouse, lever };
