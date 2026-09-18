/**
 * Loads .env for the entry points Next does not start.
 *
 * Next reads .env itself, so only the worker, drizzle-kit and one-off scripts
 * import this. In containers there is no .env — configuration arrives as real
 * environment variables — so a missing file is not an error.
 *
 * Import this for its side effect, before anything that reads `env`.
 */
try {
  process.loadEnvFile();
} catch {
  // No .env here; the process environment is expected to carry the config.
}
