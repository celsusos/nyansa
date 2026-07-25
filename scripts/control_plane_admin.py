from __future__ import annotations

import argparse
import os
import secrets
import string

from control_plane.models import (
    BootstrapRequest,
    UpdateUsagePolicyRequest,
    UpsertMemberRequest,
)
from control_plane.storage import ControlPlaneError, ControlPlaneStore
from control_plane.store_factory import create_store_from_env


DEV_ROOT_TOKEN = "dev-root-token-change-me-0001"
DEV_DEVELOPER_TOKEN = "dev-developer-token-change-me-0001"
DEV_INTERN_TOKEN = "dev-intern-token-change-me-0001"
DEV_AUDITOR_TOKEN = "dev-auditor-token-change-me-0001"


def main() -> None:
    parser = argparse.ArgumentParser(description="Control-plane admin operations")
    parser.add_argument("--database-url", default=None, help="Postgres connection string")

    subcommands = parser.add_subparsers(dest="command", required=True)

    root = subcommands.add_parser("create-root", help="Bootstrap the first root user")
    root.add_argument("--organization-name", default="Default Organization")
    root.add_argument("--root-user-id", default="root")
    root.add_argument("--root-display-name", default="Root User")
    root.add_argument("--root-token", default=os.environ.get("ROOT_TOKEN"))
    root.add_argument("--print-token", action="store_true")

    seed = subcommands.add_parser("seed-dev", help="Seed local development data")
    seed.add_argument("--organization-name", default="Dev Grid")
    seed.add_argument("--root-token", default=os.environ.get("ROOT_TOKEN", DEV_ROOT_TOKEN))
    seed.add_argument("--developer-token", default=os.environ.get("DEV_DEVELOPER_TOKEN", DEV_DEVELOPER_TOKEN))
    seed.add_argument("--intern-token", default=os.environ.get("DEV_INTERN_TOKEN", DEV_INTERN_TOKEN))
    seed.add_argument("--auditor-token", default=os.environ.get("DEV_AUDITOR_TOKEN", DEV_AUDITOR_TOKEN))

    superuser = subcommands.add_parser(
        "create-superuser", help="Create an additional platform superuser (root/platform_owner)"
    )
    superuser.add_argument("user_id")
    superuser.add_argument(
        "--platform-role", default="platform_owner", choices=["root", "platform_owner"]
    )
    superuser.add_argument("--display-name", default="")
    superuser.add_argument("--token", default=os.environ.get("SUPERUSER_TOKEN"))
    superuser.add_argument("--org", default=None, help="Attach the superuser to an organization id")

    subcommands.add_parser("show-dev-env", help="Print local development environment exports")

    args = parser.parse_args()
    if args.database_url:
        os.environ["NYANSA_DATABASE_URL"] = args.database_url
    store = create_store_from_env()

    if args.command == "create-root":
        create_root(store, args)
    elif args.command == "create-superuser":
        create_superuser(store, args)
    elif args.command == "seed-dev":
        seed_dev(store, args)
    elif args.command == "show-dev-env":
        print_dev_env()


def create_root(store: ControlPlaneStore, args: argparse.Namespace) -> None:
    root_token = args.root_token or generate_token()
    _require_token_strength(root_token)

    try:
        result = store.bootstrap(
            BootstrapRequest(
                organization_name=args.organization_name,
                root_user_id=args.root_user_id,
                root_display_name=args.root_display_name,
                root_token=root_token,
            )
        )
    except ControlPlaneError as error:
        if error.status_code == 409:
            print("Control plane is already bootstrapped; root user was not changed.")
            return
        raise

    print(f"Bootstrapped organization: {result['organization']['name']}")
    print(f"Root user: {result['root_user']['id']}")
    # Always show the token — a generated one that isn't printed leaves an
    # unusable root account.
    print(f"Root token: {root_token}")
    print("Store this token now — it cannot be recovered.")


def create_superuser(store: ControlPlaneStore, args: argparse.Namespace) -> None:
    if not store.is_bootstrapped():
        raise SystemExit("Control plane is not bootstrapped. Run create-root first.")

    token = args.token or generate_token()
    _require_token_strength(token)
    acting_root = _find_root(store)

    try:
        store.create_superuser(
            acting_root,
            user_id=args.user_id,
            token=token,
            platform_role=args.platform_role,
            display_name=args.display_name,
            organization_id=args.org,
        )
    except ControlPlaneError as error:
        raise SystemExit(str(error.detail))

    print(f"Created superuser: {args.user_id} ({args.platform_role})")
    print(f"Token: {token}")
    print("Store this token now — it cannot be recovered.")


def _find_root(store: ControlPlaneStore):
    state = store._load()
    for user in state.users.values():
        if user.platform_role in {"root", "platform_owner"}:
            return user
    raise SystemExit("No root user exists. Run create-root first.")


def seed_dev(store: ControlPlaneStore, args: argparse.Namespace) -> None:
    if not store.is_bootstrapped():
        store.bootstrap(
            BootstrapRequest(
                organization_name=args.organization_name,
                root_user_id="root",
                root_display_name="Dev Root",
                root_token=args.root_token,
            )
        )

    state = store._load()
    organization_id = next(iter(state.organizations))
    root_user = state.users["root"]

    for user_id, role, display_name, token in (
        ("dev-1", "developer", "Dev Engineer", args.developer_token),
        ("intern-1", "intern", "Dev Intern", args.intern_token),
        ("auditor-1", "auditor", "Dev Auditor", args.auditor_token),
    ):
        store.upsert_member(
            root_user,
            organization_id,
            UpsertMemberRequest(
                user_id=user_id,
                role=role,
                display_name=display_name,
                token=token,
            ),
        )

    store.update_usage_policy(
        root_user,
        "developer",
        UpdateUsagePolicyRequest(
            enabled=True,
            requests_per_minute=3,
            requests_per_hour=20,
            requests_per_day=100,
            org_requests_per_day=300,
            max_turns_per_session=30,
            max_session_minutes=20,
            max_context_tokens=64000,
        ),
    )
    store.update_usage_policy(
        root_user,
        "intern",
        UpdateUsagePolicyRequest(
            enabled=True,
            requests_per_minute=1,
            requests_per_hour=5,
            requests_per_day=20,
            org_requests_per_day=300,
            max_turns_per_session=10,
            max_session_minutes=10,
            max_context_tokens=32000,
        ),
    )

    print("Seeded development control-plane data.")
    print(f"State: {store.path}")
    print(f"Organization ID: {organization_id}")
    print("Root token: " + args.root_token)
    print("Developer token: " + args.developer_token)
    print("Intern token: " + args.intern_token)
    print("Auditor token: " + args.auditor_token)


def print_dev_env() -> None:
    print("export NYANSA_DATABASE_URL=postgresql://nyansa:nyansa@localhost:5432/nyansa")
    print("export NYANSA_CONTROL_TOKEN=" + DEV_ROOT_TOKEN)
    print("export NYANSA_BOOTSTRAP_SECRET=dev-bootstrap-secret-change-me")


def generate_token(length: int = 40) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _require_token_strength(token: str) -> None:
    if len(token) < 16:
        raise SystemExit("Root token must be at least 16 characters.")


if __name__ == "__main__":
    main()
