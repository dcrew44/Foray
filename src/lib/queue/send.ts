import "server-only";

import type { PgBoss } from "pg-boss";

import { createBoss, ensureQueues } from "./boss";
import type { QueuePayloads } from "./queues";

/**
 * The app's send-only handle on the queue.
 *
 * Next never works jobs — it enqueues and returns. The connection is cached on
 * globalThis for the same reason the database pool is: hot reload would
 * otherwise leave a pg-boss instance behind on every edit.
 */

const globalForBoss = globalThis as unknown as {
  __foraySender?: Promise<PgBoss>;
};

function connect(): Promise<PgBoss> {
  const boss = createBoss({ role: "sender" });
  boss.on("error", (error) => console.error("[queue] sender error:", error));
  return boss.start().then(async (started) => {
    await ensureQueues(started);
    return started;
  });
}

export function sender(): Promise<PgBoss> {
  globalForBoss.__foraySender ??= connect();
  return globalForBoss.__foraySender;
}

/** Enqueue a job. Returns pg-boss's job id, which runs record for cancellation. */
export async function send<K extends keyof QueuePayloads>(
  queue: K,
  payload: QueuePayloads[K],
  options?: Parameters<PgBoss["send"]>[2],
): Promise<string | null> {
  const boss = await sender();
  return boss.send(queue as string, payload as object, options ?? {});
}
