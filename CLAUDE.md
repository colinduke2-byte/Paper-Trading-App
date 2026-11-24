# PaperDesk: rules for every agent

Paper trading simulator. Python backend (`backend/`), React UI (`frontend/`). Plan: `docs/PLAN.md`. Overview: `README.md`.

## Commands
- Backend tests (run from `backend/`): `/home/user/paperdesk/backend/.venv/bin/python -m pytest -q`
  (a shared virtualenv; if a package is missing, install it there and add it to `backend/pyproject.toml`).
- Frontend (run from `frontend/`): `npm install` once, then `npm run build` (type-checks and builds).
- Run the app: `./run.sh`, then http://127.0.0.1:8000. Use a different `PORT` and `PAPERDESK_DATA` directory
  for your own test server so you never collide with another agent.

## Hard rules
- Money and quantities are `Decimal`, never `float`. Use `app.core.money`. Floats are allowed only inside
  pricing models and chart data.
- All time comes from the exchange clock (`ex.now()`), never `time.time()` in business logic.
- The ledger is the only source of truth for cash. Every cash movement is a balanced `Ledger.post`.
- Tests never touch the network. Provider code is tested against saved responses.
- No real order routing to any broker, ever. This program only simulates.
- No secrets in the repository, logs, tests or PRs. Keys come from environment variables.
- Every quote shown to the user carries its source; simulated or model data is labelled as such.
- User-facing text: plain sentences, sentence case, name the real cause in error messages.

## Working rules
- Work only on your own branch in your own worktree. Never push to `main`.
- Stay inside the files your brief gives you. If the right fix needs another file, keep the edit minimal
  and list it in your report.
- New API routes go in a new `backend/app/api/routes_<feature>.py` with `def register(app, ctx)`; it is
  loaded automatically. Use `ctx.ex` (looked up per request, it can change) and `ctx.plain` for JSON.
- New UI goes in new files under `frontend/src/`; touch `App.tsx` / `Panels.tsx` only to register it.
- Do NOT commit `frontend/dist`. Before committing run `git restore frontend/dist; git clean -fdq frontend/dist`.
  The lead rebuilds it on `main`.
- Add tests for everything new (property tests with Hypothesis for engine, ledger and margin logic).
  Never delete, skip or weaken an existing test without saying so in the report.
- The full backend suite and `npm run build` must pass before you report.
- Do not ship a guess. If something is blocked or an approach fails, say so plainly.

## Finishing
Commit, then push with `git -c push.negotiate=true push -u origin <branch>` (on HTTP 429 wait 5 s and retry once).
Open a pull request to `main` with `gh pr create -R colinduke2-byte/paper-trading-app` (do not merge it).
The description holds: what changed, evidence (test output), files touched outside your area, risks.
End the description with:

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Commit messages must not mention any model name. Your final message to the lead: PR link, head SHA,
files touched, test result, open risks, and anything you deliberately left out.
