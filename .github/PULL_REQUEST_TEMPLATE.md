## What this changes

<!-- One or two sentences. Link the issue: Fixes #123 -->

## Why

<!-- The problem this solves. Skip if the issue already covers it. -->

## How it was verified

<!-- Commands you actually ran, and what they printed. -->

- [ ] `uv run python -m unittest discover -s tests`
- [ ] `cd agent-ui && bun run typecheck && bun run build` (if UI touched)
- [ ] Postgres integration test with `TEST_DATABASE_URL` set (if `control_plane/` touched)

## Checklist

- [ ] Tests added or updated for the behaviour I changed
- [ ] README / docs updated if a user-visible flag, command, or config key changed
- [ ] No secrets, tokens, or internal hostnames in the diff
- [ ] I have read [CONTRIBUTING.md](../blob/main/CONTRIBUTING.md) and signed the [CLA](../blob/main/CLA.md)

## Security-sensitive areas

Tick any this PR touches, and say below how you verified the invariant still holds:

- [ ] `safety/sandbox.py` — no shell command escapes the sandbox
- [ ] `safety/approval.py` — no dangerous command runs without policy approval
- [ ] `safety/redaction.py` — no secret reaches the model, audit log, or UI
- [ ] `safety/audit.py` — the hash chain stays verifiable
- [ ] `agent/provenance.py` — attestations cannot be forged
- [ ] `control_plane/security.py` / `signing.py` — tokens, roles, org isolation
- [ ] `scripts/merge_gate.py` — un-attested AI code cannot pass

<!-- If you ticked any box, explain your verification here. -->
