import { createHash } from "node:crypto";
import { mkdir, readFile, unlink, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";

import { env } from "@/env";

/**
 * Where uploaded and generated files live.
 *
 * Server-side only — but note there is no `import "server-only"` here or
 * anywhere in src/lib: that package throws outside an RSC bundler, and src/lib
 * is shared by design with the worker and the scripts, which are plain Node
 * processes. The ESLint import boundaries keep server modules out of client
 * code instead.
 *
 * One implementation now (the local filesystem) behind an interface, so Phase 4
 * can swap in S3 or Blob without every caller learning about buckets. Callers
 * hold an opaque `path` and never build filesystem paths themselves.
 */

export interface Storage {
  /** Stores bytes and returns the opaque path to read them back by. */
  put(key: string, data: Buffer, contentType: string): Promise<string>;
  get(path: string): Promise<Buffer>;
  delete(path: string): Promise<void>;
}

// The fallback matters at `docker build` time: validation (and so the schema
// default) is skipped there, and this line runs when Next collects routes.
const root = resolve(env.STORAGE_DIR ?? "./data");

/** Keys are user-scoped, so one user's files cannot collide with another's. */
export function storageKey(
  userId: string,
  kind: string,
  filename: string,
): string {
  const safe = filename.replace(/[^a-zA-Z0-9._-]/g, "_").slice(-100);
  return `${userId}/${kind}/${Date.now()}-${safe}`;
}

function resolveWithin(path: string): string {
  const full = resolve(join(root, path));
  // A key containing `..` must not be able to read outside the storage root.
  if (full !== root && !full.startsWith(root + "/")) {
    throw new Error(`Refusing to access a path outside the storage root: ${path}`);
  }
  return full;
}

export const localStorage: Storage = {
  async put(key, data) {
    const full = resolveWithin(key);
    await mkdir(dirname(full), { recursive: true });
    await writeFile(full, data);
    return key;
  },

  async get(path) {
    return readFile(resolveWithin(path));
  },

  async delete(path) {
    await unlink(resolveWithin(path)).catch(() => undefined);
  },
};

export const storage: Storage = localStorage;

export function sha256(data: Buffer): string {
  return createHash("sha256").update(data).digest("hex");
}
