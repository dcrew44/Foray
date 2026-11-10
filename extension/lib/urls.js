/*
 * Which address to ask Foray about for a page or frame. Pure, and unit-tested
 * (extension/test/unit/urls.test.js).
 *
 * Nearly always the frame's own address. The exception is Greenhouse's form embedded in an
 * employer's own site: the frame is the board's page, addressed by `for` (the board) and
 * `token` (the job), and Foray knows the posting by its address on the board.
 */

const GREENHOUSE = /(^|\.)greenhouse\.io$/;

export function lookupUrl(frameUrl) {
  let url;
  try {
    url = new URL(frameUrl);
  } catch {
    return "";
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") return "";
  const board = url.searchParams.get("for");
  const job = url.searchParams.get("token");
  const embedded = GREENHOUSE.test(url.hostname) && url.pathname.startsWith("/embed/job_app");
  if (embedded && board && /^\d+$/.test(job ?? "")) {
    return `https://job-boards.greenhouse.io/${encodeURIComponent(board)}/jobs/${job}`;
  }
  return url.href;
}
