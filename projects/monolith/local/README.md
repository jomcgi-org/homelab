# Local monolith deployment

This is a lightweight, stateful fixture for exploring the SvelteKit frontend
without Kubernetes, Postgres, credentials, or production services. It starts
the mock API and the Vite frontend together. The mock database is SQLite and is
created at `local/.data/mock.sqlite3` on first run.

From the repository root:

```sh
./projects/monolith/local/run-local.sh
```

Open `http://127.0.0.1:5173` for the public homepage. Use
`http://private.localhost:5173` for the private dashboard. The two hostnames
select the corresponding SvelteKit tier, which makes both surfaces available
to Playwright without changing application code.

Ports and bind addresses can be changed without editing files:

```sh
FRONTEND_PORT=4173 API_PORT=18000 ./projects/monolith/local/run-local.sh
```

The API exposes health, homepage stats, dashboard data, tasks, and a small
knowledge graph. Task updates are persisted in SQLite, making UI interactions
useful during a browser session. Unknown API routes return a JSON 404 so a
missing fixture is visible instead of silently returning fabricated data.
