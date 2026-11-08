"""`draftassist account` and `draftassist members` (docs/shared-data.md, Stage 3). Tokens are read from a prompt that
doesn't echo, never from the command line (other programs can read a process's arguments), and never printed.

    draftassist account status
    draftassist account join dav1....                          a teammate: paste the invite
    draftassist account admin --url libsql://... --org ORG --database DB --name NAME   (asks for both tokens)
    draftassist account sign-out                               removes the stored tokens; local data stays

    draftassist members list | add NAME [--days 90] | renew NAME | remove NAME --yes
"""
from __future__ import annotations

import argparse
import getpass
import sys
from datetime import datetime


def _store():
    from draftassist import commands

    return commands.account_store()


def _when(t: float | None) -> str:
    return "never" if t is None else datetime.fromtimestamp(t).strftime("%Y-%m-%d")


def account_main() -> int:
    from draftassist.data.shared.account import InviteError

    parser = argparse.ArgumentParser(prog=sys.argv[0], description="Sign in to the team's shared data")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("status")
    join = sub.add_parser("join")
    join.add_argument("invite", nargs="?", help="the invite code (asked for if left out)")
    admin = sub.add_parser("admin")
    for flag in ("--url", "--org", "--database", "--name"):
        admin.add_argument(flag, required=True)
    sub.add_parser("sign-out")
    args = parser.parse_args(sys.argv[1:])
    store = _store()
    try:
        if args.cmd == "join":
            acc = store.join(args.invite or getpass.getpass("Invite code: "))
        elif args.cmd == "admin":
            db_token = getpass.getpass("Full-access database token: ")
            platform = getpass.getpass("Turso Platform API token: ")
            acc = store.set_admin(args.url, db_token, platform, args.org, args.database, args.name)
        elif args.cmd == "sign-out":
            acc = store.sign_out()
        else:
            acc = store.load()
    except (InviteError, ValueError) as e:
        print(e)
        return 1
    print(acc.label + (f" · {acc.url}" if acc.url else "")
          + (f" · invite expires {_when(acc.expires_at)}" if acc.role == "user" else ""))
    if acc.warning:
        print(acc.warning)
    return 0


def members_main() -> int:
    from draftassist.data.shared.members import members_for

    parser = argparse.ArgumentParser(prog=sys.argv[0], description="The team's members (admin)")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("list")
    add = sub.add_parser("add")
    add.add_argument("name")
    add.add_argument("--days", type=int, default=90)
    renew = sub.add_parser("renew")
    renew.add_argument("name")
    remove = sub.add_parser("remove")
    remove.add_argument("name")
    remove.add_argument("--yes", action="store_true", help="confirm: everyone else gets a new invite code")
    args = parser.parse_args(sys.argv[1:])
    try:
        members = members_for(_store())
        if args.cmd == "add":
            print(f"Invite for {args.name} (shown once; send it privately):\n{members.add(args.name, args.days)}")
        elif args.cmd == "renew":
            print(f"New invite for {args.name} (shown once):\n{members.renew(args.name)}")
        elif args.cmd == "remove":
            if not args.yes:
                print("Removing a member rotates the database's keys: every other member needs a new invite code. "
                      "Run again with --yes.")
                return 1
            for name, code in members.remove(args.name).items():
                print(f"{name}: {code}")
            print(f"{args.name} removed. Send each remaining member their new code.")
        else:
            for m in members.list():
                print(f"{m['name']:<20} {m['role']:<6} expires {_when(m['expires_at'])}"
                      + ("  (expiring soon)" if m["expiring"] else ""))
    except (PermissionError, ValueError, RuntimeError) as e:
        print(e)
        return 1
    return 0
