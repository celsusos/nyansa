# Licensing

Nyansa is **open core**. The agent that runs on a developer's machine is
permissively licensed. The control plane that governs a fleet of those agents
is source-available under a license that converts to Apache-2.0 over time.

## What is licensed how

| Path | License | What you may do |
| --- | --- | --- |
| `agent/`, `client/`, `config/`, `context/`, `hooks/`, `prompts/`, `safety/`, `tools/`, `ui/`, `utils/`, `evals/`, `scripts/`, `main.py`, `tests/` | [Apache-2.0](LICENSE) | Anything, including commercial use and redistribution. Includes a patent grant. |
| `control_plane/` | [BUSL-1.1](control_plane/LICENSE) | Read, modify, self-host in production for your own organization — with no cap on users or machines. You may not offer it to third parties as a competing hosted service. Converts to Apache-2.0 on **2030-07-24**. |
| `agent-ui/` | [BUSL-1.1](agent-ui/LICENSE) | Same terms as `control_plane/`. |
| `extensions/vscode-nyansa/` | [Proprietary](extensions/vscode-nyansa/LICENSE.txt) | Internal organization use with a self-hosted Nyansa deployment. |

Every source file inherits the license of the nearest `LICENSE` file above it
in the directory tree.

## In plain terms

**If you are a developer** who wants to use the Nyansa agent, embed it, fork
it, or build a product on it: the Apache-2.0 layer is yours with no strings.

**If you are a company** that wants to run the control plane for your own
engineering organization — any number of seats, in production, forever: the
Additional Use Grant covers you at no cost. You do not need to talk to us.

**If you want to resell the control plane** as a hosted or managed service to
third parties before the Change Date: you need a commercial license.
Contact <license@celsusos.com>.

## Why BUSL and not AGPL

BUSL-1.1 lets a company read and run the code without the license-compatibility
review that AGPL triggers in most enterprise legal departments, while still
preventing a cloud provider from reselling the governance layer. It is also
time-limited by design: every release becomes Apache-2.0 four years after it
ships, so nothing here is permanently locked away.

## Contributions

Contributions are accepted under the license of the directory they touch. See
[CONTRIBUTING.md](CONTRIBUTING.md) and [CLA.md](CLA.md) — a signed CLA is
required so the project can offer commercial licenses and relicense on the
Change Date.

## Trademarks

Neither license grants rights to the "Nyansa" or "Celsus" names or logos. You
may state that your product works with Nyansa; you may not name a fork Nyansa.
