/**
 * Tests import modules that read `env`, so .env has to be loaded before the
 * schema is evaluated. In CI, where there is no .env, the same variables are
 * expected in the environment.
 */
import "@/lib/load-dotenv";
