"""procrastinate tasks of this app (design §9.2, §9.3). procrastinate's Django app imports every
installed app's `tasks` module at startup; this one has none yet. Defers go through
`apps.core.queue.defer_in_tx`."""
