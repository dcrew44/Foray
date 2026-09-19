/**
 * Run an async function over a list with bounded concurrency.
 *
 * Used wherever we fan out over something we do not own — job boards, a
 * provider's API. Results keep the input order so callers can zip them back
 * against their inputs.
 */
export async function pooled<T, R>(
  items: T[],
  worker: (item: T, index: number) => Promise<R>,
  concurrency = 6,
): Promise<R[]> {
  const results: R[] = new Array(items.length);
  let cursor = 0;

  await Promise.all(
    Array.from({ length: Math.min(concurrency, items.length) }, async () => {
      for (;;) {
        const index = cursor++;
        if (index >= items.length) return;
        results[index] = await worker(items[index], index);
      }
    }),
  );

  return results;
}
