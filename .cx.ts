import "@/lib/load-dotenv";
import { pool } from "@/lib/db";
import { createRun, getRun, requestCancel } from "@/lib/runs/runs";
import { QUEUES } from "@/lib/queue/queues";
import { send } from "@/lib/queue/send";

const userId = "AkYcEgCR5vnvABvn0fIC3sZC0ODUhvTm";
const run = await createRun({ userId, kind: "search", mode: "approve_each", status: "queued", input: { prompt: "cancel test" } });
await send(QUEUES.search, { userId, runId: run.id });
console.log("queued", run.id);

// Cancel almost immediately — the worker should notice between stages.
await new Promise(r => setTimeout(r, 1200));
const cancelled = await requestCancel(userId, run.id);
console.log("cancel requested:", cancelled?.status);

await new Promise(r => setTimeout(r, 12000));
const after = await getRun(userId, run.id);
console.log("final status:", after?.status, "| stats:", JSON.stringify(after?.stats));
await pool.end();
