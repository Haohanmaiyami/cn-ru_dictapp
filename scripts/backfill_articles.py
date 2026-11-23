#!/usr/bin/env python3
"""Conservative, ID-preserving BKRS metadata backfill.

catalog: local files only. plan: repeatable-read, read-only PostgreSQL transaction.
apply/rollback: explicit database confirmation, immutable input hashes, locked
rows, durable before-image journal. No application settings or .env are read.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from backfill_legacy import project, project_reverse
from dictionary_sources import inspect_source_files, iter_source_articles

VERSION = 1
LEGACY = ("hanzi", "pinyin", "ru", "pos", "examples")
SELECT_FIELDS = "id, hanzi, pinyin, ru, pos, examples, raw_dsl, article::text AS article"


class SafetyError(RuntimeError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def file_hash(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def json_column(value):
    return None if value is None else encode(json.loads(value) if isinstance(value, str) else value)


def legacy_values(row):
    return {key: row[key] for key in LEGACY}


def payload(row):
    return (row["raw_dsl"], json_column(row["article"]))


def assert_regular(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise SafetyError("Input must be an existing regular file, not a symlink")
    return path


def private_database(path):
    """Create exclusively; a preexisting file is never silently overwritten."""
    path = Path(path)
    if not path.parent.is_dir():
        raise SafetyError("Output parent directory must already exist")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(fd)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    put_meta(connection, "format_version", VERSION)
    return connection


def read_database(path, kind):
    path = assert_regular(path).resolve()
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
        connection.close()
        raise SafetyError("Artifacts must use DELETE journal mode")
    # The long-lived shared lock pins a snapshot and prevents a SQLite writer
    # from committing changed input while an apply operation is running.
    connection.execute("BEGIN")
    if meta(connection, "format_version") != VERSION or meta(connection, "kind") != kind:
        connection.close()
        raise SafetyError("Unsupported or wrong artifact type")
    if meta(connection, "complete") is not True:
        connection.close()
        raise SafetyError("Incomplete artifact: create a new one")
    return connection


def put_meta(connection, key, value):
    connection.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, encode(value)))


def meta(connection, key):
    row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None:
        raise SafetyError("Artifact metadata is missing")
    return json.loads(row[0])


def source_manifest(directory):
    result = []
    for path in inspect_source_files(Path(directory)):
        title = None
        with path.open(encoding="utf-16") as stream:
            for line in stream:
                if line.startswith("#NAME"):
                    title = line.strip()
                if line.strip() and not line.strip().startswith("#"):
                    break
        result.append({"file": path.name, "bytes": path.stat().st_size,
                       "sha256": file_hash(path), "title": title})
    return result


def add_candidate(connection, values, raw, article, origin):
    signature = digest(values)
    previous = connection.execute("SELECT * FROM candidates WHERE signature=?", (signature,)).fetchone()
    legacy = encode(values)
    encoded_article = None if article is None else encode(article)
    if previous is None:
        connection.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?)",
                           (signature, legacy, raw, encoded_article, 0, 1, origin, None))
    else:
        same = (previous["legacy"] == legacy and previous["raw_dsl"] == raw
                and previous["article"] == encoded_article)
        connection.execute("UPDATE candidates SET occurrences=occurrences+1, ambiguous=?, conflict_origin=? WHERE signature=?",
                           (int(previous["ambiguous"] or not same), previous["conflict_origin"] if same else origin,
                            signature))


def build_catalog(directory, output, *, reverse_only=False):
    from dictapp.dsl_parser import parse_dsl_article
    from dictapp.reverse_article import parse_reverse_article
    import dictapp.dsl_parser as parser_module
    import dictapp.dsl_recovery as recovery_module
    import dictapp.reverse_article as reverse_module

    before = source_manifest(directory)
    connection = private_database(output)
    try:
        put_meta(connection, "kind", "catalog")
        put_meta(connection, "complete", False)
        put_meta(connection, "sources", before)
        put_meta(connection, "direction", "ru-zh" if reverse_only else "zh-ru")
        put_meta(connection, "parser_sha256", {
            "dsl_parser.py": file_hash(parser_module.__file__),
            "dsl_recovery.py": file_hash(recovery_module.__file__),
            "reverse_article.py": file_hash(reverse_module.__file__),
            "backfill_legacy.py": file_hash(Path(__file__).with_name("backfill_legacy.py")),
            "dictionary_sources.py": file_hash(Path(__file__).with_name("dictionary_sources.py")),
        })
        connection.execute("""CREATE TABLE candidates (
            signature TEXT PRIMARY KEY, legacy TEXT NOT NULL, raw_dsl TEXT NOT NULL,
            article TEXT, ambiguous INTEGER NOT NULL, occurrences INTEGER NOT NULL,
            origin TEXT NOT NULL, conflict_origin TEXT)""")
        counts = Counter()
        selected = {"dabruks.dsl"} if reverse_only else None
        for source in iter_source_articles(Path(directory), source_names=selected):
            counts["physical_articles"] += 1
            if source.source == "dabruks.dsl" and not reverse_only:
                counts["reverse_articles_untouched"] += 1
                continue
            article = parse_dsl_article(source.body)
            for head in source.heads:
                values = (project_reverse(head, source.body) if reverse_only else project(head, source.body))
                if values is None:
                    counts["unmappable"] += 1
                    continue
                desired_article = parse_reverse_article(head, source.body) if reverse_only else article
                if reverse_only and desired_article is None:
                    counts["unsupported_reverse_articles"] += 1
                    continue
                add_candidate(connection, values, source.body, desired_article,
                              f"{source.source}:{source.ordinal}")
                counts["bruks_candidates" if reverse_only else "bkrs_candidates"] += 1
            if counts["physical_articles"] % 10000 == 0:
                connection.commit()
            if counts["physical_articles"] % 100000 == 0:
                print(encode({"catalog_processed": counts["physical_articles"]}), flush=True)
        if source_manifest(directory) != before:
            raise SafetyError("Source files changed during catalog creation")
        counts["unique_signatures"] = connection.execute("SELECT count(*) FROM candidates").fetchone()[0]
        counts["ambiguous_signatures"] = connection.execute("SELECT count(*) FROM candidates WHERE ambiguous=1").fetchone()[0]
        put_meta(connection, "counts", dict(counts))
        put_meta(connection, "complete", True)
        connection.commit()
        return dict(counts)
    finally:
        connection.close()


def candidate_for(catalog, row):
    values = legacy_values(row)
    candidate = catalog.execute("SELECT * FROM candidates WHERE signature=?", (digest(values),)).fetchone()
    if candidate is None:
        return None, "no_exact_source"
    if candidate["ambiguous"] or candidate["legacy"] != encode(values):
        return None, "ambiguous_source"
    return candidate, None


def classify(catalog, row):
    candidate, reason = candidate_for(catalog, row)
    if reason:
        return candidate, reason
    current = payload(row)
    desired = (candidate["raw_dsl"], candidate["article"])
    if current == desired:
        return candidate, "already_current"
    if current != (None, None):
        return candidate, "populated_conflict"
    return candidate, "ready"


def connection_options(dsn_env=None):
    """Credentials remain only in process memory. No config file is loaded."""
    if dsn_env:
        value = os.environ.get(dsn_env, "")
        if not value:
            raise SafetyError("Named database URL environment variable is absent")
        parts = urlsplit(value)
        if parts.scheme not in {"postgres", "postgresql"}:
            raise SafetyError("Only a PostgreSQL URL is supported")
        # Query parameters can alter host/db; forbid them to keep identity exact.
        if parts.query or parts.fragment:
            raise SafetyError("Use DB_* variables for connection options; URL query/fragment are forbidden")
        identity = {"host": parts.hostname, "port": parts.port or 5432,
                    "database": parts.path.lstrip("/"), "user": parts.username}
        return {"dsn": value, "timeout": 20, "command_timeout": 120}, digest(identity)
    required = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")
    if any(key not in os.environ for key in required):
        raise SafetyError("Set DB_HOST, DB_PORT, DB_NAME, DB_USER and DB_PASSWORD explicitly")
    options = {"host": os.environ["DB_HOST"], "port": int(os.environ["DB_PORT"]),
               "database": os.environ["DB_NAME"], "user": os.environ["DB_USER"],
               "password": os.environ["DB_PASSWORD"], "timeout": 20, "command_timeout": 120}
    if not all(options[key] for key in ("host", "database", "user")):
        raise SafetyError("Database identity cannot be empty")
    return options, digest({key: options[key] for key in ("host", "port", "database", "user")})


async def connect(dsn_env):
    import asyncpg
    options, endpoint = connection_options(dsn_env)
    return await asyncpg.connect(**options), endpoint


async def fingerprint(db, endpoint):
    identity = dict(await db.fetchrow("""SELECT current_database() AS database, current_user AS username,
        inet_server_addr()::text AS address, inet_server_port() AS port,
        (SELECT oid::text FROM pg_database WHERE datname=current_database()) AS database_oid,
        current_setting('server_version_num') AS server_version"""))
    identity["endpoint_sha256"] = endpoint
    # Anchors use only immutable legacy fields: adding metadata won't invalidate them.
    fields = "id, hanzi, pinyin, ru, pos, examples"
    count = await db.fetchval("SELECT count(*) FROM public.entries")
    first = await db.fetch(f"SELECT {fields} FROM public.entries ORDER BY id LIMIT 32")
    last = await db.fetch(f"SELECT {fields} FROM public.entries ORDER BY id DESC LIMIT 32")
    identity["entry_count"] = count
    identity["anchor_sha256"] = digest([dict(row) for row in first + last])
    try:
        # This is stronger than db OID when the account is permitted to read it.
        async with db.transaction():
            identity["system_identifier"] = await db.fetchval("SELECT system_identifier::text FROM pg_control_system()")
    except Exception as exc:
        if type(exc).__name__ not in {"InsufficientPrivilegeError", "UndefinedFunctionError"}:
            raise
        identity["system_identifier"] = None
    return identity


async def require_schema(db):
    columns = await db.fetch("""SELECT column_name, data_type FROM information_schema.columns
        WHERE table_schema='public' AND table_name='entries'
        AND column_name IN ('raw_dsl','article')""")
    if {r["column_name"]: r["data_type"] for r in columns} != {"raw_dsl": "text", "article": "json"}:
        raise SafetyError("Additive article migration must be applied before planning")


async def make_plan(catalog_path, output, dsn_env=None, batch_size=1000):
    catalog = read_database(catalog_path, "catalog")
    catalog_hash = file_hash(catalog_path)
    plan = private_database(output)
    db = None
    try:
        put_meta(plan, "kind", "plan")
        put_meta(plan, "complete", False)
        put_meta(plan, "catalog_sha256", catalog_hash)
        put_meta(plan, "sources", meta(catalog, "sources"))
        plan.execute("CREATE TABLE planned (id INTEGER PRIMARY KEY, signature TEXT NOT NULL, old_raw TEXT, old_article TEXT)")
        db, endpoint = await connect(dsn_env)
        counts = Counter()
        examples = []
        async with db.transaction(isolation="repeatable_read", readonly=True):
            await require_schema(db)
            put_meta(plan, "target", await fingerprint(db, endpoint))
            async for row in db.cursor(f"SELECT {SELECT_FIELDS} FROM public.entries ORDER BY id", prefetch=batch_size):
                candidate, reason = classify(catalog, row)
                counts[reason] += 1
                if reason == "ready":
                    plan.execute("INSERT INTO planned VALUES (?,?,?,?)", (row["id"], candidate["signature"], *payload(row)))
                elif reason != "already_current" and len(examples) < 50:
                    examples.append({"id": row["id"], "reason": reason})
                if sum(counts.values()) % 10000 == 0:
                    plan.commit()
        if file_hash(catalog_path) != catalog_hash:
            raise SafetyError("Catalog changed during planning")
        put_meta(plan, "counts", dict(counts))
        put_meta(plan, "skipped_examples", examples)
        put_meta(plan, "complete", True)
        plan.commit()
        return dict(counts)
    finally:
        if db is not None:
            await db.close()
        catalog.close()
        plan.close()


def open_journal(path, plan_hash, catalog_hash, target):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        journal = private_database(path)
        put_meta(journal, "kind", "journal")
        put_meta(journal, "complete", True)
        put_meta(journal, "plan_sha256", plan_hash)
        put_meta(journal, "catalog_sha256", catalog_hash)
        put_meta(journal, "target", target)
        journal.execute("""CREATE TABLE changes (
            id INTEGER PRIMARY KEY, signature TEXT NOT NULL, old_raw TEXT, old_article TEXT,
            new_raw TEXT NOT NULL, new_article TEXT, state TEXT NOT NULL)""")
        journal.commit()
    else:
        checked = read_database(path, "journal")
        checked.close()
        journal = sqlite3.connect(assert_regular(path))
        journal.row_factory = sqlite3.Row
        journal.execute("PRAGMA synchronous=FULL")
        if (meta(journal, "plan_sha256") != plan_hash or meta(journal, "catalog_sha256") != catalog_hash
                or meta(journal, "target") != target):
            journal.close()
            raise SafetyError("Journal belongs to a different plan/catalog/database")
    return journal


def transition(current, before, after, has_journal, rollback=False):
    """Pure crash-recovery decision; unknown metadata is never overwritten."""
    if rollback:
        if not has_journal:
            raise SafetyError("Rollback requires a durable before-image")
        if current == before:
            return "already_restored"
        if current == after:
            return "restore"
    else:
        if current == before:
            return "write"
        if has_journal and current == after:
            return "already_applied"
    raise SafetyError("Row changed since plan: refusing to overwrite metadata")


@contextmanager
def process_lock(journal_path):
    """Prevent two apply/rollback processes from sharing one journal."""
    import fcntl
    path = Path(str(journal_path) + ".lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SafetyError("Another process is using this journal") from None
        yield
    finally:
        os.close(fd)


async def mutate(catalog_path, plan_path, journal_path, confirmed_database,
                 dsn_env=None, batch_size=200, rollback=False):
    catalog = read_database(catalog_path, "catalog")
    plan = read_database(plan_path, "plan")
    catalog_hash = file_hash(catalog_path)
    plan_hash = file_hash(plan_path)
    db = journal = None
    try:
        if meta(plan, "catalog_sha256") != catalog_hash:
            raise SafetyError("Catalog hash differs from approved plan")
        expected = meta(plan, "target")
        if confirmed_database != expected["database"]:
            raise SafetyError("Explicit database confirmation does not match plan")
        db, endpoint = await connect(dsn_env)
        async with db.transaction(isolation="repeatable_read", readonly=True):
            await require_schema(db)
            current = await fingerprint(db, endpoint)
            # Rollback remains usable if an unrelated entry was added/edited.
            # Database identity and each touched row's content are still checked.
            ignored = {"entry_count", "anchor_sha256", "server_version"} if rollback else set()
            if {k: v for k, v in current.items() if k not in ignored} != {k: v for k, v in expected.items() if k not in ignored}:
                raise SafetyError("Database identity, row count or legacy anchor content changed")
        with process_lock(journal_path):
            if rollback and not Path(journal_path).is_file():
                raise SafetyError("Rollback journal does not exist")
            journal = open_journal(journal_path, plan_hash, catalog_hash, expected)
            source = journal.execute("SELECT id, signature, old_raw, old_article FROM changes ORDER BY id") if rollback else plan.execute("SELECT * FROM planned ORDER BY id")
            counts = Counter()
            while batch := source.fetchmany(batch_size):
                updates = []
                decisions = []
                async with db.transaction():
                    await db.execute("SET LOCAL lock_timeout = '5s'")
                    rows = await db.fetch(f"SELECT {SELECT_FIELDS} FROM public.entries WHERE id=ANY($1::int[]) ORDER BY id FOR UPDATE", [r["id"] for r in batch])
                    by_id = {row["id"]: row for row in rows}
                    for item in batch:
                        row = by_id.get(item["id"])
                        if row is None:
                            raise SafetyError("Planned row disappeared")
                        candidate, reason = candidate_for(catalog, row)
                        if reason or candidate["signature"] != item["signature"]:
                            raise SafetyError("Legacy row content changed since plan")
                        old = (item["old_raw"], item["old_article"])
                        new = (candidate["raw_dsl"], candidate["article"])
                        recorded = journal.execute("SELECT * FROM changes WHERE id=?", (item["id"],)).fetchone()
                        if recorded is not None and (recorded["signature"] != item["signature"]
                                or (recorded["old_raw"], recorded["old_article"]) != old
                                or (recorded["new_raw"], recorded["new_article"]) != new):
                            raise SafetyError("Journal before/after image differs from plan")
                        action = transition(payload(row), old, new, recorded is not None, rollback)
                        decisions.append((item["id"], action))
                        if action in {"write", "restore"}:
                            updates.append((*(old if rollback else new), item["id"]))
                        if recorded is None:
                            journal.execute("INSERT INTO changes VALUES (?,?,?,?,?,?,?)",
                                            (item["id"], item["signature"], *old, *new, "pending"))
                    # Commit and fsync before-images BEFORE committing any PostgreSQL change.
                    journal.commit()
                    if updates:
                        await db.executemany("UPDATE public.entries SET raw_dsl=$1, article=$2::json WHERE id=$3", updates)
                        written = await db.fetch(f"SELECT {SELECT_FIELDS} FROM public.entries WHERE id=ANY($1::int[]) ORDER BY id", [update[2] for update in updates])
                        wanted = {entry_id: (raw, article) for raw, article, entry_id in updates}
                        if len(written) != len(updates) or any(
                                legacy_values(row) != legacy_values(by_id[row["id"]])
                                or payload(row) != wanted[row["id"]] for row in written):
                            raise SafetyError("Post-write verification failed; database transaction rolled back")
                # A crash before this commit leaves pending records that are safe to replay.
                journal.executemany("UPDATE changes SET state=? WHERE id=?",
                                    [("rolled_back" if rollback else "applied", row_id) for row_id, _ in decisions])
                journal.commit()
                counts.update(action for _, action in decisions)
                print(encode({"progress": dict(counts)}), flush=True)
            if file_hash(catalog_path) != catalog_hash or file_hash(plan_path) != plan_hash:
                raise SafetyError("Input artifact changed during operation; keep journal and investigate")
            return dict(counts)
    finally:
        if db is not None:
            await db.close()
        if journal is not None:
            journal.close()
        catalog.close()
        plan.close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    catalog = commands.add_parser("catalog", help="Build private local source catalog; no DB connection")
    catalog.add_argument("--source-dir", required=True)
    catalog.add_argument("--output", required=True)
    catalog.add_argument("--reverse-only", action="store_true",
                         help="Recover existing BRuKS rows only; preserve IDs and all legacy fields")
    plan = commands.add_parser("plan", help="Read-only dry run; does not change PostgreSQL")
    plan.add_argument("--catalog", required=True)
    plan.add_argument("--output", required=True)
    plan.add_argument("--dsn-env", help="Name of env var containing DSN; otherwise explicit DB_* env")
    plan.add_argument("--batch-size", type=int, default=1000)
    for verb in ("apply", "rollback"):
        command = commands.add_parser(verb, help="Explicit guarded metadata write" if verb == "apply" else "Guarded restore from journal")
        command.add_argument("--catalog", required=True)
        command.add_argument("--plan", required=True)
        command.add_argument("--journal", required=True)
        command.add_argument("--confirm-database", required=True)
        command.add_argument("--dsn-env")
        command.add_argument("--batch-size", type=int, default=200)
    inspect = commands.add_parser("inspect", help="Print artifact counts/hash; no DB connection")
    inspect.add_argument("path")
    inspect.add_argument("--kind", choices=("catalog", "plan", "journal"), required=True)
    args = parser.parse_args()
    if hasattr(args, "batch_size") and not 1 <= args.batch_size <= 5000:
        parser.error("batch-size must be between 1 and 5000")
    try:
        if args.command == "catalog":
            result = build_catalog(args.source_dir, args.output, reverse_only=args.reverse_only)
        elif args.command == "plan":
            result = asyncio.run(make_plan(args.catalog, args.output, args.dsn_env, args.batch_size))
        elif args.command in {"apply", "rollback"}:
            result = asyncio.run(mutate(args.catalog, args.plan, args.journal, args.confirm_database,
                                        args.dsn_env, args.batch_size, args.command == "rollback"))
        else:
            artifact = read_database(args.path, args.kind)
            result = {"kind": args.kind, "sha256": file_hash(args.path)}
            if args.kind != "journal":
                result["counts"] = meta(artifact, "counts")
            else:
                result["states"] = dict(artifact.execute("SELECT state, count(*) FROM changes GROUP BY state").fetchall())
            artifact.close()
        print(encode({"status": "ok", "result": result}))
    except SafetyError as exc:
        print("STOP: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        # Database exceptions may embed connection strings/passwords. Never echo them.
        print("STOP: operation failed (" + type(exc).__name__ + "). No credential values printed.", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
