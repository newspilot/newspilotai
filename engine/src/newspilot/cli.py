# SPDX-License-Identifier: AGPL-3.0-only
"""Command-line administration.

  newspilot db upgrade                          run migrations (as NEWSPILOT_MIGRATION_DATABASE_URL)
  newspilot tenant create SLUG "Name"           add a publication
  newspilot tenant list                         list active publications
  newspilot secret new-master-key               print a new value for NEWSPILOT_MASTER_KEY
  newspilot secret set SLUG NAME                store a secret; the value is read from standard input
  newspilot secret list SLUG                    list secrets (names, status, fingerprints; never values)
  newspilot setting list SLUG                   show the site's settings, with their help text
  newspilot setting set SLUG KEY VALUE          change a setting ("none" clears it back to its default)

Secret values are never accepted as command-line arguments, because those end up in shell history and process
lists.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import textwrap

from newspilot import config, migrations, settings, settings_registry
from newspilot.db import Tenant, make_engine, make_session_factory
from newspilot.secret_store import LocalKeyProvider, SecretStore, new_master_key
from newspilot.tenancy import TenantContext, active_tenant_ids, create_tenant, tenant_id_for_slug, tenant_session


def _sessions():
    return make_session_factory(make_engine(config.database_url()))


def _actor() -> str:
    return f"cli:{getpass.getuser()}"


def _context(sessions, slug: str) -> TenantContext:
    tenant_id = tenant_id_for_slug(sessions, slug)
    if tenant_id is None:
        raise SystemExit(f"no tenant with slug {slug!r}")
    return TenantContext(tenant_id, actor=_actor())


def _read_secret(name: str) -> str:
    interactive = sys.stdin.isatty()
    value = (getpass.getpass(f"Value for {name} (input hidden): ") if interactive else sys.stdin.read()).strip()
    if not value:
        raise SystemExit("no value given")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="newspilot", description="NewsPilot AI administration")
    sub = parser.add_subparsers(dest="group", required=True)

    db = sub.add_parser("db").add_subparsers(dest="cmd", required=True)
    db.add_parser("upgrade", help="run database migrations")

    tenant = sub.add_parser("tenant").add_subparsers(dest="cmd", required=True)
    t_create = tenant.add_parser("create", help="add a publication")
    t_create.add_argument("slug")
    t_create.add_argument("name")
    tenant.add_parser("list", help="list active publications")

    secret = sub.add_parser("secret").add_subparsers(dest="cmd", required=True)
    secret.add_parser("new-master-key", help="print a new master key")
    s_set = secret.add_parser("set", help="store a secret (value from standard input)")
    s_set.add_argument("slug")
    s_set.add_argument("name")
    s_list = secret.add_parser("list", help="list a tenant's secrets")
    s_list.add_argument("slug")

    setting = sub.add_parser("setting").add_subparsers(dest="cmd", required=True)
    g_list = setting.add_parser("list", help="show settings with their help text")
    g_list.add_argument("slug")
    g_set = setting.add_parser("set", help="change a setting")
    g_set.add_argument("slug")
    g_set.add_argument("key")
    g_set.add_argument("value")

    args = parser.parse_args(argv)

    if args.group == "db":
        migrations.upgrade()
        print("database is up to date")
    elif args.group == "tenant" and args.cmd == "create":
        tenant_id = create_tenant(_sessions(), args.slug, args.name)
        print(f"created tenant {args.slug} ({tenant_id})")
    elif args.group == "tenant" and args.cmd == "list":
        sessions = _sessions()
        for tenant_id in active_tenant_ids(sessions):
            with tenant_session(sessions, TenantContext(tenant_id, actor=_actor())) as session:
                row = session.get(Tenant, tenant_id)
                print(f"{row.slug}\t{row.name}\t{tenant_id}")
    elif args.group == "secret" and args.cmd == "new-master-key":
        print(new_master_key())
    elif args.group == "secret" and args.cmd == "set":
        sessions = _sessions()
        store = SecretStore(LocalKeyProvider.from_env())
        with tenant_session(sessions, _context(sessions, args.slug)) as session:
            info = store.put(session, args.name, _read_secret(args.name))
        print(f"stored {info.name} (fingerprint {info.fingerprint}, status {info.status})")
    elif args.group == "secret" and args.cmd == "list":
        sessions = _sessions()
        store = SecretStore(None)  # listing never decrypts, so it needs no master key
        with tenant_session(sessions, _context(sessions, args.slug)) as session:
            for info in store.list(session):
                print(f"{info.name}\t{info.status}\t{info.fingerprint}\t{info.updated_at:%Y-%m-%d %H:%M}")
    elif args.group == "setting" and args.cmd == "list":
        sessions = _sessions()
        with tenant_session(sessions, _context(sessions, args.slug)) as session:
            views = settings.describe(session)
        print(render_settings(views))
    elif args.group == "setting" and args.cmd == "set":
        sessions = _sessions()
        if settings_registry.definition(args.key) is None:
            raise SystemExit(f"unknown setting {args.key!r}; see `newspilot setting list {args.slug}`")
        try:
            with tenant_session(sessions, _context(sessions, args.slug)) as session:
                settings.put(session, args.key, args.value)
                value = settings.get(session, args.key)
        except settings.InvalidSettingError as e:
            raise SystemExit(str(e)) from e
        print(f"{args.key} = {_show(value)}")
    return 0


def _show(value) -> str:
    if value is None or value == {}:
        return "not set (no limit)"
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in value.items())
    return str(value)


def render_settings(views) -> str:
    wrap = textwrap.TextWrapper(width=100, initial_indent="    ", subsequent_indent="    ")
    out = []
    groups = {}
    for v in views:
        groups.setdefault(v.definition.group, []).append(v)
    for group, items in groups.items():
        title, intro = settings_registry.GROUPS.get(group, (group, ""))
        out += [title.upper(), ""]
        out += [wrap.fill(p) + "\n" for p in intro.split("\n\n") if p]
        for v in items:
            out.append(f"  {v.definition.label}  [{v.definition.key}]")
            out.append(f"    current: {_show(v.value)}")
            out.append(wrap.fill(v.definition.help))
            out.append("")
    return "\n".join(out).rstrip() + "\n"


if __name__ == "__main__":
    sys.exit(main())
