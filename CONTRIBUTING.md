# Contributing to Nyansa

Thanks for considering it. This guide covers what you need to know before
opening a pull request.

## Before you start

- **Security issues do not go here.** Follow [SECURITY.md](SECURITY.md).
- **Large changes:** open an issue describing the approach before writing code.
  A rejected 2,000-line PR is a bad day for everyone.
- **Licensing:** contributions are accepted under the license of the directory
  you touch — Apache-2.0 for the agent, BUSL-1.1 for `control_plane/` and
  `agent-ui/`. See [LICENSING.md](LICENSING.md).
- **CLA:** a signed Contributor License Agreement is required before we can
  merge. See [CLA.md](CLA.md). The bot will prompt you on your first PR.

## Development setup

Requires Python 3.12, [uv](https://docs.astral.sh/uv/), Node 22, and
[Bun](https://bun.sh/) (UI only).

```bash
uv sync --dev                       # Python deps
uv run nyansa-init --quick          # generate local secrets + .env
make dev                            # control plane + UI
```

Never commit the `.env` that step writes — `.gitignore` covers `.env*`, and CI
runs a secret scan that will fail the build if something slips through.

## Running tests

```bash
uv run python -m unittest discover -s tests      # full suite
uv run python -m unittest tests.test_sandbox     # one module

cd agent-ui && bun run typecheck && bun run build
```

The Postgres-backed integration test is skipped unless `TEST_DATABASE_URL` is
set. CI sets it, so a test that passes locally can still fail there — run it
yourself if you touched `control_plane/postgres_storage.py`:

```bash
docker run --rm -d -p 5432:5432 \
  -e POSTGRES_USER=nyansa -e POSTGRES_PASSWORD=nyansa -e POSTGRES_DB=nyansa \
  postgres:18
TEST_DATABASE_URL=postgresql://nyansa:nyansa@localhost:5432/nyansa \
  uv run python -m unittest tests.test_postgres_store
```

## Pull requests

- One logical change per PR
- Add or update tests — every module in `tests/` maps to a subsystem; put yours
  in the matching file
- Update the README if you changed a user-visible flag, command, or config key
- Keep the existing style: type hints on public functions, no new runtime
  dependencies without discussion

## Areas that need extra care

These subsystems carry the product's security guarantees. Changes here get
reviewed harder and need tests that specifically prove the guarantee still
holds:

| Area | The invariant you must not break |
| --- | --- |
| `safety/sandbox.py` | No shell command escapes the platform sandbox |
| `safety/approval.py` | No dangerous command runs without the configured policy allowing it |
| `safety/redaction.py` | No secret reaches the model, the audit log, or the UI |
| `safety/audit.py` | The hash chain stays verifiable across every write path |
| `agent/provenance.py` | An attestation cannot be produced for code the agent did not write |
| `control_plane/security.py`, `signing.py` | Tokens, roles, and org isolation hold under every request path |
| `scripts/merge_gate.py` | Un-attested AI code cannot pass the gate |

If you are changing one of these and are not sure what the invariant means,
ask in the issue first.

## Commit messages

Short imperative subject line, body explaining *why* if it is not obvious:

```
Reject attestations with a mismatched workspace digest

The gate compared the signature but not the tree it covered, so a
valid attestation from one commit passed for another.
```
