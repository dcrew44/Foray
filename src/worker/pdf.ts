import { chromium, type Browser, type LaunchOptions } from "playwright-core";

import { env } from "@/env";

/**
 * HTML in, PDF out, with headless Chromium.
 *
 * The only module in the codebase that imports Playwright: the worker renders
 * PDFs and Next never does (ESLint enforces the boundary). One browser is
 * launched lazily and shared; every render gets its own context, so renders
 * running side by side cannot see each other's pages, and a context is cheap
 * where a browser launch is not.
 */

const LAUNCH: LaunchOptions = {
  headless: true,
  // The worker owns shutdown. Playwright's own signal handlers would close the
  // browser the moment Ctrl-C arrives — and on SIGINT exit the process with
  // code 130 — in the middle of the worker draining its in-flight jobs. Its
  // process-exit hook, which kills the browser when the worker finally exits,
  // is always installed and is the backstop.
  handleSIGINT: false,
  handleSIGTERM: false,
  handleSIGHUP: false,
};

const RENDER_TIMEOUT_MS = 30_000;

let browserPromise: Promise<Browser> | null = null;
let inflight = 0;
const idle: Array<() => void> = [];
let hooksInstalled = false;

/**
 * Which browser to run.
 *
 * The bundled Chromium is what the production image (the Playwright base
 * image) ships. In development the bundled browsers are not downloaded, so a
 * missing executable falls back to the locally installed Chrome.
 * `PDF_BROWSER_CHANNEL` forces a channel ("chrome", "msedge", …) outright.
 */
async function launch(): Promise<Browser> {
  const channel = env.PDF_BROWSER_CHANNEL?.trim();
  if (channel) return chromium.launch({ ...LAUNCH, channel });

  try {
    return await chromium.launch(LAUNCH);
  } catch (error) {
    if (!isMissingExecutable(error)) throw error;
    return chromium.launch({ ...LAUNCH, channel: "chrome" });
  }
}

function browser(): Promise<Browser> {
  if (browserPromise) return browserPromise;

  const pending = launch().then((launched) => {
    // A crash or an external close drops the cached handle, so the next
    // render launches a fresh browser instead of failing forever.
    launched.on("disconnected", () => {
      if (browserPromise === pending) browserPromise = null;
    });
    return launched;
  });
  pending.catch(() => {
    if (browserPromise === pending) browserPromise = null;
  });

  browserPromise = pending;
  installShutdownHooks();
  return pending;
}

/**
 * Renders a complete HTML document to a Letter PDF.
 *
 * The page size and margins come from the document's own `@page` rule
 * (`preferCSSPageSize`), so the template, not this function, owns layout.
 */
export async function renderPdf(
  html: string,
): Promise<{ pdf: Buffer; pages: number }> {
  inflight += 1;
  try {
    try {
      return await renderOnce(html);
    } catch (error) {
      if (!isBrowserGone(error)) throw error;
      // The browser died under us (crash, OOM kill, closed elsewhere).
      // Relaunch once; a second failure is a real error.
      console.warn("[pdf] browser went away mid-render; relaunching once");
      await discard();
      return await renderOnce(html);
    }
  } finally {
    inflight -= 1;
    if (inflight === 0) for (const resolve of idle.splice(0)) resolve();
  }
}

async function renderOnce(html: string): Promise<{ pdf: Buffer; pages: number }> {
  let instance = await browser();
  if (!instance.isConnected()) {
    await discard();
    instance = await browser();
  }

  // A resume page loads nothing and runs nothing. Scripts are off and every
  // request is refused, so even markup that slipped past escaping could not
  // fetch a URL or execute while the PDF is being made.
  const context = await instance.newContext({ javaScriptEnabled: false });
  try {
    await context.route("**/*", (route) => route.abort());
    const page = await context.newPage();
    page.setDefaultTimeout(RENDER_TIMEOUT_MS);

    await page.setContent(html, { waitUntil: "load" });
    const pdf = await page.pdf({
      format: "Letter",
      printBackground: true,
      preferCSSPageSize: true,
    });

    return { pdf, pages: countPdfPages(pdf) };
  } finally {
    await context.close().catch(() => undefined);
  }
}

/**
 * Page count straight from the PDF's bytes.
 *
 * Chromium writes an uncompressed page tree, so each page is a literal
 * `/Type /Page` object (the `/Type /Pages` tree nodes are excluded). The
 * root's `/Count` is the fallback for a PDF that compresses its objects.
 */
export function countPdfPages(pdf: Uint8Array): number {
  const raw = Buffer.from(pdf.buffer, pdf.byteOffset, pdf.byteLength).toString(
    "latin1",
  );
  const pages = raw.match(/\/Type\s*\/Page(?![A-Za-z])/g)?.length ?? 0;
  if (pages > 0) return pages;

  const counts = [...raw.matchAll(/\/Count\s+(\d+)/g)].map((m) => Number(m[1]));
  return counts.length > 0 ? Math.max(...counts) : 0;
}

/** Closes the shared browser now. For scripts and tests; the worker uses the hooks below. */
export async function closeBrowser(): Promise<void> {
  const pending = browserPromise;
  browserPromise = null;
  if (!pending) return;
  const instance = await pending.catch(() => null);
  await instance?.close().catch(() => undefined);
}

async function discard(): Promise<void> {
  await closeBrowser();
}

/**
 * Closes the browser once in-flight renders finish, or after a bound.
 *
 * Waiting matters because the worker drains its jobs on SIGINT/SIGTERM before
 * exiting: closing immediately would fail a render that the drain was about
 * to let complete. A render that starts after this simply relaunches, and
 * Playwright's exit hook kills that browser when the process exits.
 */
async function closeWhenIdle(maxWaitMs = 25_000): Promise<void> {
  if (inflight > 0) {
    await new Promise<void>((resolve) => {
      const timer = setTimeout(resolve, maxWaitMs);
      timer.unref();
      idle.push(() => {
        clearTimeout(timer);
        resolve();
      });
    });
  }
  await closeBrowser();
}

/**
 * Adds to, rather than replaces, whatever signal handling the process has.
 * In the worker its own handlers drain and exit; in a plain script with no
 * other listener, the signal is re-raised after closing so it still exits.
 */
function installShutdownHooks() {
  if (hooksInstalled) return;
  hooksInstalled = true;

  for (const signal of ["SIGINT", "SIGTERM"] as const) {
    process.once(signal, () => {
      void closeWhenIdle().finally(() => {
        if (process.listenerCount(signal) === 0) process.kill(process.pid, signal);
      });
    });
  }
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function isMissingExecutable(error: unknown): boolean {
  return /Executable doesn't exist|executable doesn't exist|Failed to launch.*ENOENT/i.test(
    message(error),
  );
}

function isBrowserGone(error: unknown): boolean {
  return /has been closed|Target closed|browser has disconnected|Browser closed|crashed/i.test(
    message(error),
  );
}
