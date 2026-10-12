/**
 * The resume upload's size limit, checked in the browser before anything is sent.
 *
 * Django refuses a file over 10 MB with a friendly 413, but only if the request reaches it: Next's
 * rewrite buffers at most `proxyClientMaxBodySize` (16 MB, next.config.ts) and then forwards a
 * truncated body under the original length, so a larger upload hangs until the proxy's 10-minute
 * timeout. Checking here gives every oversized file Django's own sentence at once. The rule and
 * the wording are `apps/resumes/parse/sniff.py`'s (`MAX_UPLOAD_BYTES`, `too_large_message`).
 */

export const RESUME_MAX_BYTES = 10 * 1024 * 1024;

/** Django's refusal for a file of `size` bytes, or null when the size is allowed. */
export function resumeTooLarge(size: number): string | null {
  if (size <= RESUME_MAX_BYTES) return null;
  return `That file is ${(size / 1024 / 1024).toFixed(1)} MB; the limit is 10 MB.`;
}
