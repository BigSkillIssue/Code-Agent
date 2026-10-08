"""`forge-web user …`: manage accounts from the server's command line."""

import argparse
import asyncio
import sys
import time

from sqlalchemy import select

from forge_web.auth import onetime
from forge_web.auth.passwords import hash_password, password_problem
from forge_web.db.engine import Database
from forge_web.db.models import User
from forge_web.settings import WebSettings


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """The user options."""
    actions = parser.add_subparsers(dest="action", required=True)
    add = actions.add_parser("add", help="create an account")
    add.add_argument("--email", required=True)
    add.add_argument("--name", default="")
    add.add_argument("--admin", action="store_true", help="make the account an admin")
    add.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    reset = actions.add_parser("reset-link", help="print a link that sets a new password")
    reset.add_argument("--email", required=True)
    actions.add_parser("list", help="list accounts")


def run(settings: WebSettings, args: argparse.Namespace) -> int:
    """Run one user action."""
    return asyncio.run(_run(settings, args))


async def _run(settings: WebSettings, args: argparse.Namespace) -> int:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.database_url())
    await db.migrate()
    try:
        if args.action == "add":
            return await add_user(db, settings, args)
        if args.action == "reset-link":
            return await reset_link(db, settings, args.email.strip().lower())
        return await list_users(db)
    finally:
        await db.close()


async def add_user(db: Database, settings: WebSettings, args: argparse.Namespace) -> int:
    """Create an account; without a password, print a link to set one."""
    import secrets

    email = args.email.strip().lower()
    password = sys.stdin.readline().rstrip("\n") if args.password_stdin else None
    if password is not None and (problem := password_problem(password)):
        print(f"error: password: {problem}", file=sys.stderr)
        return 1
    async with db.session() as session, session.begin():
        if await session.scalar(select(User).where(User.email == email)) is not None:
            print(f"error: an account with {email} exists already", file=sys.stderr)
            return 1
        user = User(
            id=secrets.token_hex(16), email=email, name=args.name or email.split("@")[0],
            role="admin" if args.admin else "member", status="active", created_at=time.time(),
            email_verified=True, password_hash=await hash_password(password) if password else None,
        )  # fmt: skip
        session.add(user)
    print(f"created {user.role} {email}")
    if password is None:
        await reset_link(db, settings, email)
    return 0


async def reset_link(db: Database, settings: WebSettings, email: str) -> int:
    """Print a link that lets the user choose a new password (valid for 2 hours)."""
    async with db.session() as session:
        user = await session.scalar(select(User).where(User.email == email))
    if user is None:
        print(f"error: no account with {email}", file=sys.stderr)
        return 1
    token = await onetime.issue(db, "reset", user_id=user.id)
    print(f"set a password within 2 hours: {settings.base_url()}/reset#token={token}")
    return 0


async def list_users(db: Database) -> int:
    """Print every account."""
    async with db.session() as session:
        users = list(await session.scalars(select(User).order_by(User.created_at)))
    for user in users:
        print(f"{user.email or '-':40} {user.role:7} {user.status}")
    return 0
