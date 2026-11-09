# Contributing

Use Python 3.12+, uv and Node 22+. Run `uv sync --frozen` and `npm ci`.
Before opening a pull request:

```sh
uv run pytest -q
npm test
npm run test:workers
cd examples/worker && npx wrangler deploy --dry-run
```

Changes to the API must update `docs/openapi.json` with
`uv run python scripts/export_openapi.py` and keep the TypeScript client in sync.
Cover failure paths and duplicate requests when changing persistence or submission.
No Modal credentials are required for the unit suite. Live tests are opt-in and
incur ordinary Modal compute usage.

Keep `.env`, Modal credentials and real job payloads out of commits and issues.
