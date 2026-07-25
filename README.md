# Nyansa

**Self-hosted infrastructure for running and governing AI coding agents across a
team — not a single coding assistant, but the platform that puts one in every
developer's terminal under central control.**

You host the control plane (Docker + Postgres); developers connect their local CLI
to it with one command. The organization decides **who** can use AI, **which tools**
each role gets, **how much**, and **which model** — and gets a tamper-evident record
of **everything the AI did**.

## What it is — two layers

**🧑‍💻 The agent — runs on each developer's machine**
A capable terminal AI coding agent: it plans and carries out multi-step tasks with a
full tool suite (read / write / edit files, run shell commands, search your code,
browse the web, remember context, track a to-do list), extends itself through **MCP
servers** and specialist **subagents**, and runs against **OpenAI-compatible**
models, **Anthropic's API**, or a local **Claude Code** subscription. It checks its
own work by re-running your tests, and risky commands run inside a sandbox with your
approval.

**🛡️ The control plane — the infrastructure you host**
A Postgres-backed FastAPI service and Quasar/Vue dashboard that governs every agent
across your organization:

- **Sign-in & provisioning** — members sign in with a username/password or
  **Microsoft (Entra) SSO**. With SSO on, new employees are **auto-provisioned** on
  first sign-in — mapped to an organization by email domain and given a default
  role — so onboarding needs no manual account creation.
- **People & access** — create organizations, add members, assign roles (root,
  owner, security-admin, developer, intern, auditor), and rotate or revoke access.
- **What each role can do** — choose which agent tools each role may use, so (for
  example) interns can read and search but never run shell commands.
- **How much they can use** — set per-role limits on requests per minute / hour /
  day, session length, and context size, to keep cost and risk in check.
- **Which model they use** — set the organization's model from the dashboard and
  every developer's agent picks it up (each provider API key stays in its own
  environment; the control plane never stores it).
- **Live oversight** — watch active AI sessions and revoke any of them instantly.
- **A trustworthy record** — every action is written to a tamper-evident,
  hash-chained audit log you can verify with one click.
- **Proof of AI-written code** — each AI change is **Ed25519-signed**, and a merge
  gate can block un-attested AI code from reaching your main branch.

Developers connect their terminal to the control plane with one command
(`nyansa-init --join <url>`) or by issuing a CLI token from the dashboard; the
agent then enforces their role, tool policy, and usage caps on every run.

Under the hood it's a Python CLI plus an optional **Postgres-backed FastAPI control
plane** and a **Quasar/Vue operator UI** — with password + Microsoft-SSO auth, a
bundled TLS proxy, and per-IP rate limiting for production. Deeper engine features —
context compaction, loop detection, save/resume workspace checkpoints, secret
redaction, hooks, an evaluation harness (`nyansa-eval`), and a **VS Code
extension** — are covered further down.

---

## 🚀 Quick start

**New here? This is the whole setup.** You install two tools, then run one command.
No prior experience needed — copy and paste each block.

### 1. Install the two tools it needs

- **uv** — runs the app and manages Python for you (no separate Python install):

  ```bash
  # macOS / Linux
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
  ```powershell
  # Windows (PowerShell)
  powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
  ```

- **Docker Desktop** — runs the database and web dashboard. Install it from
  [docker.com](https://www.docker.com/products/docker-desktop) and **make sure it's
  open and running** before the next steps.

### 2. Get the code and install it

```bash
git clone https://github.com/<your-username>/nyansa.git
cd nyansa
uv sync
```

### 3. Run the setup wizard

```bash
uv run nyansa-init
```

Answer a few questions — press **Enter** to accept each default. The wizard does the
rest: it starts the database, creates your root login, and writes your settings to
`.env`. **When it finishes it prints the root password and recovery/CLI token —
copy them somewhere safe, because stored tokens can't be recovered.**

### 4. Start using it

```bash
source .env        # loads your settings
uv run nyansa
```

Type a request at the `>` prompt. Your **control center** is the web dashboard at
**http://localhost:8080** — add teammates, set what each role can do, cap usage, and
review the audit log (see [§7](#7-control-plane--operator-ui--manage-who-uses-ai-and-how)).

> On **Windows PowerShell**, replace `source .env` with:
> `Get-Content .env | ForEach-Object { if ($_ -match '^\s*([^#=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($matches[1], $matches[2]) } }`

> **Which model?** The default is a **free** OpenRouter coding model
> (`poolside/laguna-s-2.1:free`). "Free" means no per-token charge, but you still
> need a free [OpenRouter key](https://openrouter.ai/settings/keys) — free models
> are rate-limited per account, so heavy use may hit caps (switch to a paid model
> like `openai/gpt-4o-mini` for higher limits). Free-model availability rotates; if
> the default stops working, pick another from
> [openrouter.ai/models?max_price=0](https://openrouter.ai/models?max_price=0).
> Prefer **100% local with no key and no limits**? Install
> [Ollama](https://ollama.com), run `ollama pull qwen2.5-coder:7b`, and choose
> **Ollama** in the wizard.

**That's the whole thing.** Everything below is optional detail for customizing the
model, running without Docker, deploying to a server, or understanding the layout.

---

## 1. Prerequisites (reference)

Install these before you start:

| Tool | Version | Purpose | Install |
| --- | --- | --- | --- |
| **Python** | 3.12 (>=3.11) | Agent runtime | pinned in `.python-version` |
| **uv** | latest | Python env + dependency manager | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **Docker** | latest | Runs the API/UI service and Postgres | Docker Desktop or Docker Engine |
| **A model endpoint** | — | OpenAI-compatible `/v1/chat/completions` or Anthropic Messages API | Installer defaults to OpenRouter-compatible config |

Optional:

| Tool | Version | Purpose |
| --- | --- | --- |
| **Node** | 22 (via [`nvm`](https://github.com/nvm-sh/nvm)) | Build/run the Quasar UI |
| **Bun** | latest | UI package manager & dev server |
| **Postgres** | 16 | Use your own database instead of the Compose container |
| **Claude Code** | latest | Optional local Claude subscription runtime |

> The control plane runs on **Postgres in both dev and production** so the builds
> match. You don't need to install Postgres by hand — Docker Compose starts a
> private Postgres container for local/private installs.

---

## 2. Get the code

```bash
git clone https://github.com/<your-username>/nyansa.git
cd nyansa
```

---

## 3. Install dependencies

```bash
uv sync
```

This creates a `.venv` and installs everything from `pyproject.toml` / `uv.lock`.
Add `--dev` if you plan to run the test suite:

```bash
uv sync --dev
```

---

## Fastest path: the one-command installer

`nyansa-init` is the production-style first-run installer. It generates
secrets, writes `.env`, starts Docker Compose, creates the Postgres-backed
control plane, bootstraps your first **organization and root account** through
the running API, writes `.nyansa/config.toml`, and leaves both CLI and UI ready
to use.

If you do not choose a model, the installer uses the app default: the **free**
coding model `poolside/laguna-s-2.1:free` through `https://openrouter.ai/api/v1`.
You still need a free OpenRouter key (free models are rate-limited per account) —
add it during setup or set `NYANSA_API_KEY` in `.env` afterward.

```bash
uv run nyansa-init          # guided; press Enter to accept each default
uv run nyansa-init --quick  # Docker + Postgres + default OpenRouter-compatible model
```

It's idempotent — re-running reuses your existing org, root login, token, and
secrets instead of overwriting them. When it finishes it prints the root password
and recovery/CLI token (also saved to `.env`); store them safely.

After setup:

```bash
source .env
uv run nyansa
```

Open the UI at http://localhost:8080.

Prefer to wire things up by hand? The manual steps follow.

### Joining a team's deployment (for everyone after the first person)

The setup above is for the **admin** who stands up the control plane. Once that's
running, **each teammate** connects their own machine with one command — no Docker,
no Postgres, no bootstrap:

```bash
git clone https://github.com/<your-username>/nyansa.git
cd nyansa && uv sync
uv run nyansa-init --join https://control-plane.your-company.com
```

It asks for the **username and password your admin gave you**, signs you in, issues
a long-lived CLI token, and writes your `.env` + `.nyansa/config.toml` pointed at
the team's control plane. Then:

```bash
source .env
uv run nyansa            # run /capabilities to see what your role allows
```

Your role, allowed tools, and usage limits come from the control plane — you don't
configure any of that locally.

---

## 4. Point the agent at a model

**The default is [OpenRouter](https://openrouter.ai) — one key for GPT, Claude,
Gemini, Llama, and more.** The installer sets this up for you; to change it later,
edit three lines in `.env`:

```bash
BASE_URL=https://openrouter.ai/api/v1          # the endpoint
NYANSA_MODEL=poolside/laguna-s-2.1:free      # free coding model (default); any id works
NYANSA_API_KEY=<your-openrouter-key>         # from openrouter.ai/settings/keys
```

For local-only use, that's it — `.env` is the source of truth and the agent picks
it up on the next run. With the control plane enabled, root/platform owners manage
an approved model catalog in the operator UI's **Model** page, and org admins choose
one enabled catalog entry for their organization. The CLI syncs the selected model
name, provider, endpoint, and API-key requirement from `/me` on startup; your local
`.env` still supplies the actual API key.

> **Advanced:** `.nyansa/config.toml` `[model]` holds finer settings
> (`temperature`, `context_window`, `provider`, `requires_api_key`). Values in
> `.env` (`NYANSA_MODEL`, `BASE_URL`) override the model name and endpoint there
> for local-only runs; an org-wide catalog selection in the UI overrides both when
> control-plane sync is enabled.

The sections below are **opt-in alternatives** to OpenRouter — you don't need them
for the default setup.

### Local Ollama Alternative

```bash
ollama pull qwen2.5-coder:7b
```

```bash
BASE_URL=http://localhost:11434/v1
NYANSA_MODEL=qwen2.5-coder:7b
NYANSA_API_KEY=local
```

Point `base_url` at any OpenAI-compatible gateway: vLLM, LM Studio, OpenRouter,
or an internal company gateway. For providers that require auth, keep
`requires_api_key = true` and put the real key in the env var named by
`api_key_env`.

### Native Anthropic API mode

Use this when the user or organization has an Anthropic API key:

```bash
export ANTHROPIC_API_KEY="..."
```

```toml
[model]
provider = "anthropic"
name = "claude-sonnet-4-20250514"
base_url = "https://api.anthropic.com"
api_key_env = "ANTHROPIC_API_KEY"
requires_api_key = true
max_output_tokens = 4096
context_window = 200000
```

The runtime translates this app's existing tool loop to Anthropic Messages API
`tools`, `tool_use`, and `tool_result` blocks, then maps streaming text, tool
calls, stop reasons, and token usage back into the common agent event stream.

### Claude subscription mode

Claude.ai Pro/Max subscriptions are separate from Anthropic API billing. Do not
use a Claude subscription as an Anthropic API key. To let a developer use their
paid Claude subscription, install Claude Code on that developer machine and run
`claude` once to authenticate. Then configure the agent to invoke Claude Code in
headless mode:

```toml
[model]
provider = "claude_code"
name = "sonnet"
requires_api_key = false
claude_code_command = "claude"
claude_code_permission_mode = "default"
```

Claude Code mode uses the local `claude -p --output-format stream-json` runtime.
Claude Code performs its own coding/tool loop with the user's local subscription
login; this agent still registers the governed session, checks usage policy before
invocation, detects workspace changes afterward, runs verification, and records
attestations for changed files when the control plane is enabled.

For centralized provider billing or the normal in-process tool loop, route Claude
through an OpenAI-compatible company gateway instead.

Config resolution: a system-level config (in your OS config dir) is loaded first,
then `.nyansa/config.toml` in the project is merged on top. Project settings win.

---

## 5. Run the agent

**Interactive session** (a REPL in your terminal):

```bash
uv run nyansa
```

Type a request in plain English (e.g. *"add tests for the auth module"*) and the
agent works in the current directory.

You can also type a **command** starting with `/` — a shortcut for things like
checking what you're allowed to do or saving your work. Type `/help` any time to
see the list. The most useful ones:

| Command | What it does |
| --- | --- |
| `/help` | Show all commands |
| `/whoami` | Who you're signed in as (user, role, organization) |
| `/capabilities` | The tools **you** can use and your usage limits |
| `/tools` | List the tools available this session |
| `/model [name]` | Show the current model, or switch it |
| `/approval [mode]` | Show or change how much the agent asks before acting |
| `/save` · `/resume` · `/sessions` | Save this conversation, resume a saved one, or list them |
| `/checkpoint` · `/restore` | Snapshot your files, or roll them back to a snapshot |
| `/stats` | Token usage and session stats |
| `/audit verify` | Check the tamper-proof activity log is intact |
| `/config` · `/mcp` | View your config · list connected MCP tool servers |
| `/clear` | Clear the conversation |
| `/exit` (or `/quit`) | Leave |

Not sure what your role lets you do? Run **`/capabilities`** — it lists your
allowed tools and limits (the same thing the dashboard shows).

**Single-shot** (run one prompt and exit — good for scripting):

```bash
uv run nyansa "add a docstring to every public function in utils/"
```

**Run against a different directory:**

```bash
uv run nyansa --cwd /path/to/your/project "fix the failing tests"
```

That's the whole loop for local use — install, point at a model, run.

---

## 6. Run the tests (optional)

```bash
uv run python -m unittest discover -s tests
# or
make test
```

---

## 7. Control plane + operator UI — manage who uses AI, and how

This is the control center. Open **http://localhost:8080** after setup and you can:

- **add people and assign roles** (developer, intern, auditor, admin, …),
- **choose which agent tools each role may use**,
- **set usage limits per role** (requests per minute/hour/day, session length,
  context size),
- **watch live AI sessions and revoke any of them**,
- **review and verify the tamper-evident audit log**, and
- **check signed proof of which code the AI wrote**.

Every agent run then authenticates against this service, so those rules are
enforced no matter where the CLI runs.

Under the hood: Compose runs the FastAPI API, the built Quasar UI, and Postgres.
The CLI reads `.nyansa/config.toml` and talks to the same API for identity, role
policy, audit events, and usage controls.

The fastest path:

```bash
make dev-setup
source .env
uv run nyansa
```

Then open:

- **API + UI:** http://127.0.0.1:8080
- **Health:** http://127.0.0.1:8080/health
- **Readiness:** http://127.0.0.1:8080/ready

Sign in to the UI with the root user/password printed by the installer. From
**Setup → Connect CLI**, issue your own CLI token and export it as
`NYANSA_CONTROL_TOKEN`; UI sessions and CLI tokens are separate credentials.

The installer writes this block into `.nyansa/config.toml` so the CLI is
governed automatically:

```toml
[control_plane]
enabled = true
base_url = "http://localhost:8080"
token_env = "NYANSA_CONTROL_TOKEN"
fail_closed = true
sync_actor = true
send_audit_events = true
```

The CLI then fetches the actor/role from `/me` at startup, enforces that role's tool
policy, and forwards audit events to `/audit/events`.

### Production Role Workflows

The UI includes a role-aware **Workbench**:

| Role | Production workflow |
| --- | --- |
| `root` | Platform readiness, organization creation, global role/usage policy, audit verification, superuser recovery. |
| `platform_owner` | Same platform operations as root without being the bootstrap identity. |
| `org_owner` | Member onboarding, organization model selection, session oversight, and org-scoped tool policies for `developer` / `intern` / `auditor`. |
| `security_admin` | Member/token review, active-session revocation, intern escalation triage, audit review, and org-scoped restricted-role policies. |
| `developer` | Microsoft/password login, self-service CLI token issuance, governed coding sessions, own session/usage visibility. |
| `intern` | Restricted safe tools, guided CLI setup, own session visibility, and escalation requests when more access is needed. |
| `auditor` | Read-only audit/session/attestation visibility and exportable audit evidence packages. |

Platform policies are defaults. Org owners and security admins can override tool
access for restricted roles inside their organization without gaining global
platform policy control.

### Production Governance

Before launch, use the operator UI's **Governance** page to close the remaining
platform controls:

- Set an org network policy so agents inherit whether shell networking is allowed,
  which domains are allowed or blocked, and whether a proxy is required.
- Register enabled integrations for source control, ticketing, chat, identity,
  SIEM, SCIM/SAML, MCP, or custom internal connectors. Store only non-secret
  connector metadata here; keep tokens in your secret manager.
- Use approval requests for elevated tools, model changes, network destinations,
  managed workspaces, and integrations. Decisions are written into the audit chain.
- Check the production readiness score. A 10/10 requires bootstrap, approved
  models, org model selections, org network policies, role and usage policies, a
  valid audit chain, organization domains for Microsoft sign-in, enabled
  integrations, and no pending approvals.

### Manual Docker Run

```bash
cp .env.example .env
# set NYANSA_BOOTSTRAP_SECRET, NYANSA_TOKEN_PEPPER,
# NYANSA_ATTESTATION_KEY_SEED, POSTGRES_PASSWORD,
# NYANSA_ROOT_PASSWORD, and NYANSA_API_KEY in .env first
COMPOSE_BAKE=true docker compose up --build      # or: make docker-up
```

Compose starts both the API/UI service and a private Postgres container. The
runtime control plane requires Postgres in local and production deployments; it
does not fall back to local JSON state. In production mode `/ready` also requires
a bootstrap secret, a persistent token pepper, and a persistent attestation key
seed.

Bootstrap the first root user against the running API:

```bash
curl -X POST http://localhost:8080/bootstrap \
  -H 'content-type: application/json' \
  -H "x-bootstrap-secret: $NYANSA_BOOTSTRAP_SECRET" \
  -d '{"organization_name":"Acme","root_user_id":"root","root_password":"change-this-root-password"}'

# Then sign in at http://localhost:8080 and issue a CLI token from Setup → Connect CLI.
```

For production, store `.env` values in your secret manager or hosting provider
environment. The control plane stores organization/account/governance state in
Postgres; model API keys are process secrets.

### Microsoft Sign-In

The UI supports Microsoft Entra ID / Microsoft identity platform sign-in through
OpenID Connect authorization-code flow. Register a **Web** app in Microsoft Entra
and add this redirect URI:

```text
https://<your-control-plane-domain>/auth/microsoft/callback
```

Then set:

```bash
NYANSA_MICROSOFT_ENABLED=true
NYANSA_MICROSOFT_TENANT=<tenant-id-or-organizations>
NYANSA_MICROSOFT_CLIENT_ID=<application-client-id>
NYANSA_MICROSOFT_CLIENT_SECRET=<client-secret>
NYANSA_MICROSOFT_REDIRECT_URI=https://<your-control-plane-domain>/auth/microsoft/callback
NYANSA_MICROSOFT_ALLOWED_DOMAINS=company.example
NYANSA_MICROSOFT_DOMAIN_ORG_MAP=company.example:Acme
NYANSA_MICROSOFT_AUTO_PROVISION=true
NYANSA_MICROSOFT_DEFAULT_ROLE=developer
```

When enabled, the login screen shows **Sign in with Microsoft**. The server
validates the Microsoft ID token against Microsoft discovery/JWKS, checks the
email domain allowlist, resolves the user's organization by domain, creates or
maps the user, and sets an HttpOnly app session cookie. Users can then issue
their own CLI token from **Setup → Connect CLI**.

For multi-org production, configure routing in one of these ways:

- set `NYANSA_MICROSOFT_DOMAIN_ORG_MAP=domain:org-id-or-name,...`
- or create organizations with Microsoft domains in the UI

If more than one organization exists and a Microsoft user's domain cannot be
resolved, sign-in fails closed instead of silently adding them to the wrong org.

### Production hardening

Built in and on by default:

- **TLS termination** — enable the bundled Caddy reverse proxy (automatic HTTPS):
  ```bash
  DOMAIN=cp.yourcompany.com docker compose --profile tls up --build
  ```
  Caddy provisions a real certificate for `DOMAIN` and proxies to the app; the app
  runs with `--proxy-headers` and emits HSTS behind TLS. **Don't also publish
  `control-plane:8080` to the internet — reach it only through Caddy.**
- **Rate limiting** — per-IP sliding window on every endpoint (health/readiness
  exempt); returns `429` + `Retry-After`. Tune with `NYANSA_RATE_LIMIT_PER_MINUTE`
  (default `120`, `0` disables). It's per-process; front it with a shared limiter
  (proxy/Redis) if you run multiple workers and need a hard global cap.
- **Token expiry** — issued member CLI tokens expire by default after
  `NYANSA_TOKEN_TTL_HOURS` (default `720` = 30 days; `0` = never). The root
  bootstrap/recovery token is exempt to avoid lockout — rotate it deliberately.
  UI login sessions are separate 12-hour bearer sessions.
- **Access logging** — structured, secret-free request logs (`method path status
  dur_ms ip`) on the `control_plane` logger.
- **CI** — `.github/workflows/ci.yml` runs the full test suite against a real
  Postgres service plus the UI typecheck/build on every push and PR.

**Not yet production-grade — plan for these before company-wide rollout:**

| Gap | Status / workaround |
| --- | --- |
| **Datastore scaling** | The Postgres store persists the whole control-plane state as one JSONB row under a global write lock — fine for a team, but it won't scale to high audit/usage volume. Normalizing into per-entity tables is the top follow-up. |
| **MFA / identity lifecycle** | Microsoft Entra OIDC sign-in is built in. Enforce MFA, conditional access, and joiner/mover/leaver policy in Entra; this app maps/provisions users and roles after Microsoft authenticates them. |
| **Secret management** | Secrets live in `.env` / env vars; wire them to Vault or a cloud KMS for production. |
| **HA / horizontal scale** | Single-writer by design (global lock); run one control-plane instance and back up Postgres + the pepper/attestation keys. |
| **Sandbox strength** | The agent's shell sandbox needs `bubblewrap` on Linux for a real boundary; the `rlimit` fallback is a resource guard only. |

### Deploy on Render

The repo includes `render.yaml` for a Docker web service plus managed Postgres.
Set `BASE_URL`, `NYANSA_MODEL`, and `NYANSA_API_KEY` in Render after the
blueprint is created, then use the generated `NYANSA_BOOTSTRAP_SECRET` once to
bootstrap the root user.

### VS Code extension

An extension under `extensions/vscode-nyansa` adds a Nyansa activity-bar panel
with an Assistant chat surface, governed terminal launch, control-plane
identity/sessions, and workspace environment controls. The panel and terminal use
the same CLI command, env file, Python interpreter setting, and extra env values.

```bash
make dev-setup
make vscode-check
# Open extensions/vscode-nyansa in VS Code and press F5 to launch a dev host.
```

For local/private rollout:

```bash
make vscode-install
```

For production org rollout, publish the extension to Visual Studio Marketplace,
Open VSX, or your internal artifact registry and distribute the `.vsix` through
your normal device-management/developer-portal flow. Users still need the CLI
installed, a control-plane token, and `nyansa.controlPlaneUrl` pointed at your
control-plane URL.

---

## Project layout

| Path | What it is |
| --- | --- |
| `main.py` | CLI entry point (`nyansa`) |
| `agent/` | Core agent loop, sessions, persistence, workspace snapshots |
| `tools/` | Built-in tools + MCP integration |
| `client/`, `context/` | Model client and context/compaction management |
| `config/` | Config schema and loader |
| `safety/` | Approval policies, sandbox, role policy |
| `control_plane/` | FastAPI control plane (roles, audit, usage, attestations) |
| `agent-ui/` | Quasar/Vue operator dashboard |
| `evals/` | Evaluation harness (`nyansa-eval`) |
| `extensions/vscode-nyansa/` | VS Code extension |
| `scripts/` | Admin tooling, merge gate (`nyansa-gate`) |

### Console scripts

Defined in `pyproject.toml`, all runnable via `uv run <name>`:

- `nyansa` — the CLI agent
- `nyansa-init` — interactive first-run installer (model, secrets, org + root account, CLI config)
- `nyansa-control-plane` — FastAPI control plane server
- `nyansa-control-admin` — control-plane admin CLI (create-root / create-superuser / seed-dev)
- `nyansa-eval` — evaluation harness
- `nyansa-gate` — CI merge gate for signed provenance

#### Creating superusers

`nyansa-control-admin` is the operator CLI (like Django's `manage.py`) for
platform-level actions. It talks straight to the datastore via
`NYANSA_DATABASE_URL` (or `--database-url`), no running server required.

```bash
# The first superuser + first organization (one-time bootstrap). Prints the token.
uv run nyansa-control-admin create-root --organization-name "Acme"

# Additional platform superusers, any time after bootstrap.
uv run nyansa-control-admin create-superuser alice --platform-role platform_owner
uv run nyansa-control-admin create-superuser --help   # all options
```

Each command prints the generated login token **once** — store it, it can't be
recovered. Pass `--token` to set your own.

---

## Key features

- **Tools:** repo map, read/write/edit files, glob & grep search, shell execution,
  web search/fetch, memory, and todo planning. Search honours `.gitignore`, skips
  vendored/build dirs, and truncates output so one call can't exhaust context.
- **Verification loop:** after file-modifying turns, a configurable test command runs
  and failures are fed back for repair (auto-detects `pytest`, `npm test`,
  `cargo test`, `go test ./...`, `make test`, …).
- **Context management:** automatic compaction near token limits; the original request
  is pinned and never summarised away.
- **Safety:** approval policies (on-request / auto / never / yolo), dangerous-command
  detection, and a per-platform shell sandbox (bubblewrap on Linux, sandbox-exec on
  macOS, rlimit fallback).
- **Sessions & checkpoints:** save/resume sessions; checkpoints snapshot the whole
  workspace byte-exact and restore reverts edits, deletions, and creations.
- **MCP & subagents:** connect to MCP servers (stdio + HTTP/SSE); built-in and
  user-defined subagents from `.nyansa/subagents/*.toml`.
- **Enterprise controls:** actor metadata, JSONL audit logging with content hashing,
  secret redaction, role-based tool policy and usage caps, and Ed25519-signed
  provenance attestations enforceable as a CI merge gate.

### Project instructions

`AGENTS.md` (legacy: `AGENT.md`) is loaded from the working directory up to the repo
root and concatenated root-first, so the most deeply nested file wins on conflict.
Place one in your repo to give the agent standing instructions.

---

## Troubleshooting

- **`DATABASE_URL or NYANSA_DATABASE_URL is required`** — run
  `uv run nyansa-init --quick` or set Postgres env vars before starting the API.
- **`Configuration Error` on startup** — check `[model]` in `.nyansa/config.toml`
  and that `.env` has a valid `BASE_URL` / `NYANSA_API_KEY`.
- **Connection refused / timeouts** — make sure your model server is running and
  reachable at `base_url` (for Ollama: `ollama serve` and `ollama list`).
- **Docker Compose Bake crashes locally** — run with `COMPOSE_BAKE=false`; the
  regular Docker builder is supported too.
- **Empty/garbled responses or wrong token counts** — set `context_window` to match
  the model you actually serve; a few older servers also need
  `supports_stream_options = false` in `[model]`.
- **`make dev` UI errors** — ensure Node 22 via `nvm use 22` and that Bun is installed.

---

## Licensing

Nyansa is **open core**:

- **The agent** (`agent/`, `tools/`, `safety/`, `context/`, `client/`, `hooks/`,
  `ui/`, `evals/`, `main.py`, …) is **Apache-2.0**. Use it commercially, fork it,
  embed it — no strings.
- **The control plane and operator UI** (`control_plane/`, `agent-ui/`) are
  **BUSL-1.1**. You may read, modify, and **self-host them in production for your
  own organization at any scale, free of charge**. You may not resell them as a
  competing hosted service before the Change Date (2030-07-24), after which each
  release becomes Apache-2.0.

Full detail in [LICENSING.md](LICENSING.md). Commercial licenses and hosted
deployments: <license@celsusos.com>.

## Contributing

Pull requests welcome — start with [CONTRIBUTING.md](CONTRIBUTING.md). A signed
[CLA](CLA.md) is required so the project can offer commercial licenses and
relicense to Apache-2.0 on the Change Date. All participants are held to the
[Code of Conduct](CODE_OF_CONDUCT.md).

Before your first commit, install the local guard rails — they run the same
secret scan CI does:

```bash
uv tool install pre-commit && pre-commit install
```

## Security

Nyansa runs AI agents with shell access inside other people's development
environments. **Do not report vulnerabilities in public issues** — use the
Security tab → *Report a vulnerability*. Scope, response targets, and
self-hosting expectations are in [SECURITY.md](SECURITY.md).

## Sponsorship

If Nyansa saves your team time, sponsorship funds maintenance, security fixes,
and the docs. See the ❤️ *Sponsor* button at the top of the repository.
