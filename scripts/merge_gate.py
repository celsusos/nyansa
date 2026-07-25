from __future__ import annotations

import os
from pathlib import Path
import sys

import click
import httpx

from agent.provenance import (
    branch,
    changed_files,
    commit_sha,
    hash_text,
    repository_name,
)


def _hash_file(path: Path) -> str | None:
    try:
        return hash_text(path.read_text("utf-8", errors="replace"))
    except OSError:
        return None


@click.command()
@click.option("--base-url", default=lambda: os.environ.get("NYANSA_CONTROL_BASE_URL", "http://localhost:8080"))
@click.option("--token-env", default="NYANSA_CONTROL_TOKEN", help="Env var holding the bearer token.")
@click.option("--base-ref", default=None, help="Compare against this ref (default: HEAD~1).")
@click.option("--require-policy-version", default=None)
@click.option("--repo", default=None, help="Override the detected repository name.")
@click.option("--cwd", default=".", type=click.Path(exists=True, file_okay=False))
def main(base_url, token_env, base_ref, require_policy_version, repo, cwd):
    """Fail CI when AI-touched files in this commit range are not attested."""
    workdir = Path(cwd).resolve()
    repository = repo or repository_name(workdir)
    sha = commit_sha(workdir)

    file_hashes: dict[str, str] = {}
    for rel in changed_files(workdir, base_ref):
        digest = _hash_file(workdir / rel)
        if digest is not None:
            file_hashes[rel] = digest

    token = os.environ.get(token_env)
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    payload = {
        "repository": repository,
        "commit_sha": sha,
        "file_hashes": file_hashes,
        "require_policy_version": require_policy_version,
    }

    try:
        response = httpx.post(
            f"{base_url.rstrip('/')}/gate/check",
            headers=headers,
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        click.echo(f"gate: could not reach control plane: {error}", err=True)
        sys.exit(2)

    result = response.json()

    click.echo(f"repository:  {result['repository']}")
    click.echo(f"commit:      {sha or 'unknown'} ({branch(workdir) or 'detached'})")
    click.echo(f"chain valid: {result['chain_valid']}")
    click.echo(f"attestations matched: {result['matched_attestations']}")
    click.echo(f"changed files checked: {len(file_hashes)}")

    for path in result.get("uncovered_files", []):
        click.echo(f"  unattested: {path}")
    for path in result.get("mismatched_files", []):
        click.echo(f"  hash mismatch: {path}")

    if result["passed"]:
        click.echo("gate: PASS")
        sys.exit(0)

    click.echo("gate: FAIL", err=True)
    for reason in result.get("reasons", []):
        click.echo(f"  - {reason}", err=True)
    sys.exit(1)


if __name__ == "__main__":
    main()
