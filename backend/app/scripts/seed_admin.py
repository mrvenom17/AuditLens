"""Create or reset an account from the command line (TASK-009).

There is no self-registration anywhere in this system (01_REQUIREMENTS.md
§ User Authentication, Business Rules), so the first Admin has to come from
somewhere outside the API. This script is that somewhere, and TASK-009 requires
it to stay a manual operation — it is deliberately not exposed as an endpoint.

It also carries the password reset path. 05_SECURITY.md §10.2 rules out
self-service recovery for the POC and says an Admin resets manually via a
documented internal procedure; this is that procedure, which is better than the
"direct database action" 01_REQUIREMENTS.md contemplated because it goes through
the same hashing code the login path verifies against.

Usage:
    python -m app.scripts.seed_admin create --email a@firm.com --name "Ada" --role admin
    python -m app.scripts.seed_admin reset-password --email a@firm.com
    python -m app.scripts.seed_admin list
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import string
import sys

from app.auth.password import hash_password
from app.db.session import session_scope
from app.models.enums import Role
from app.repositories.user import SessionRepository, UserRepository

MIN_PASSWORD_LENGTH = 12


def _generate_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(24))


def _prompt_password() -> str:
    """Read a password without echoing it, and never from argv.

    A password passed as a command-line argument lands in the shell history and
    in the process list, which is exactly the persistent-log exposure TASK-009's
    security requirement rules out.
    """
    first = getpass.getpass("Password (leave blank to generate one): ")
    if not first:
        generated = _generate_password()
        # Printed to the terminal only. The caller is expected to hand it over
        # out of band and have the recipient change it.
        print(f"\nGenerated password: {generated}\n", file=sys.stderr)
        return generated
    if len(first) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if first != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords did not match.")
    return first


def create_user(email: str, name: str, role: Role) -> None:
    with session_scope() as db:
        users = UserRepository(db)
        if users.get_by_email(email) is not None:
            raise SystemExit(f"An account already exists for {email}.")
        password = _prompt_password()
        user = users.create(
            email=email, password_hash=hash_password(password), name=name, role=role
        )
        print(f"Created {role.value} account {user.email} (id {user.id}).")


def reset_password(email: str) -> None:
    with session_scope() as db:
        users = UserRepository(db)
        user = users.get_by_email(email)
        if user is None:
            raise SystemExit(f"No account found for {email}.")
        user.password_hash = hash_password(_prompt_password())
        # A password reset that leaves existing sessions alive is not a reset:
        # if the reason for resetting was a suspected compromise, the attacker's
        # cookie would keep working.
        revoked = SessionRepository(db).revoke_all_for_user(user.id)
        print(f"Password reset for {user.email}. Revoked {revoked} active session(s).")


def list_users() -> None:
    with session_scope() as db:
        for user in UserRepository(db).list_users():
            state = "active" if user.is_active else "inactive"
            print(f"{user.email:<40} {user.role.value:<10} {state}")


def main() -> None:
    parser = argparse.ArgumentParser(description="AuditLens account management.")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="create an account")
    create.add_argument("--email", required=True)
    create.add_argument("--name", required=True)
    create.add_argument(
        "--role", required=True, choices=[r.value for r in Role], help="auditor, reviewer or admin"
    )

    reset = sub.add_parser("reset-password", help="reset an account's password")
    reset.add_argument("--email", required=True)

    sub.add_parser("list", help="list accounts")

    args = parser.parse_args()
    if args.command == "create":
        create_user(args.email, args.name, Role(args.role))
    elif args.command == "reset-password":
        reset_password(args.email)
    else:
        list_users()


if __name__ == "__main__":
    main()
