# Foray

An AI-first job search agent. Upload a resume, answer a short interview, then ask for
what you want — *"entry-level software engineering jobs in the Bay Area"* — and
background agents search many sources, rank every posting against your profile, and
tailor your resume per job.

Status: **Phase 1 in progress** (find, rank, tailor). See [DESIGN.md](DESIGN.md) for the
settled decisions and architecture, and [CLAUDE.md](CLAUDE.md) for the working
conventions.

## Quickstart

> Filled in as the pieces land. Requires Node 24+, pnpm 10, and Docker Desktop.

```bash
pnpm install
cp .env.example .env     # set DATABASE_URL, BETTER_AUTH_SECRET, and one LLM provider key
pnpm db:up               # Postgres 18 in Docker
pnpm db:migrate
pnpm dev:all             # Next.js + worker
```

Open http://localhost:3000.
