# Security Policy

Nyansa runs AI agents with shell access inside other people's development
environments, and stores the audit record those organizations rely on. We treat
security reports as the highest-priority class of issue.

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Report privately through GitHub: go to the **Security** tab → **Report a
vulnerability** (GitHub Private Vulnerability Reporting). If that is
unavailable to you, email <license@celsusos.com> with `[SECURITY]` in the subject.

Please include:

- The affected component (agent CLI, control plane, operator UI, VS Code extension)
- Version or commit SHA
- Reproduction steps or a proof of concept
- What an attacker gains — the impact matters more than the CVSS score

### What to expect

| Stage | Target |
| --- | --- |
| Acknowledgement of your report | 48 hours |
| Initial assessment and severity | 5 business days |
| Fix or documented mitigation for critical issues | 30 days |
| Public disclosure | Coordinated with you, after a fix ships |

We will credit you in the advisory unless you ask us not to. There is no paid
bounty program at this time.

## Supported versions

Nyansa is pre-1.0. Only the latest release on `main` receives security fixes.

## Scope

In scope — we want to hear about these:

- **Sandbox escape** from the shell tool (bubblewrap, `sandbox-exec`, or the
  rlimit fallback)
- **Approval bypass** — any path that executes a dangerous command without the
  configured approval policy being honoured
- **Audit log forgery** — breaking the hash chain, or writing entries that
  verify but misrepresent what happened
- **Attestation forgery** — producing a valid Ed25519 provenance signature for
  code the agent did not write, or defeating the merge gate
- **Authentication and authorization** in the control plane: token issuance,
  session revocation, SSO auto-provisioning, role and tool-policy enforcement,
  organization isolation (any cross-tenant data access)
- **Secret leakage** — API keys, token pepper, or attestation seed reaching
  logs, audit events, model prompts, or the operator UI
- **Redaction bypass** in `safety/redaction.py` for realistic secret formats

Out of scope:

- Findings that require an attacker to already have the control plane's
  `NYANSA_TOKEN_PEPPER` or `NYANSA_ATTESTATION_KEY_SEED`
- The agent executing instructions from a repository the operator explicitly
  pointed it at, when the approval policy is set to `yolo` or `never`
- Rate limiting or DoS against a deployment you do not own
- Vulnerabilities in models or model providers rather than in Nyansa
- Missing hardening headers on a local, non-production `make dev` deployment

## Deployment security expectations

If you self-host the control plane, these are your responsibility. Nyansa
cannot enforce them for you:

- Generate `NYANSA_BOOTSTRAP_SECRET`, `NYANSA_TOKEN_PEPPER`,
  `NYANSA_ATTESTATION_KEY_SEED`, `POSTGRES_PASSWORD`, and
  `NYANSA_ROOT_PASSWORD` yourself; never reuse the values from `.env.example`
- Keep `token_pepper.key` and `attestation_signing_key.pem` on the host only —
  they are gitignored for a reason, and rotating them invalidates existing
  tokens and attestations
- Run with `NYANSA_PRODUCTION=true` and terminate TLS in front of the API
- Set `fail_closed = true` in `[control_plane]` so agents refuse to run when
  the control plane is unreachable, rather than running unpoliced
- Restrict direct network access to Postgres

See [docs/production-readiness.md](docs/production-readiness.md).

## Prompt injection

Nyansa is an agent with tool access. Content it reads — source files, web pages,
MCP server responses — can attempt to steer it. The approval policy, tool
policy, and sandbox are the controls that contain this; they are defence in
depth, not a guarantee. Do not grant shell or write tools to roles that operate
on untrusted repositories, and do not run with `approval = "yolo"` outside a
disposable environment. Reports showing a **bypass of those controls** are in
scope; reports showing that an agent followed injected instructions **within**
its granted permissions are expected behaviour.
