import json
import os
import sqlite3
import time

from data import VENDORS

DB = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "app.db"))

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("pragma busy_timeout=5000")
    return c

def init():
    c = conn()
    try:
        _init_schema(c)
    finally:
        c.close()

def _init_schema(c):
    with c:
        c.executescript("""
        create table if not exists runs(
            id text primary key, created_at real, filename text, source text, file_hash text,
            vendor_id text, po_id text, invoice_number text, invoice_date text, total real,
            action text, recommendation text, status text, decision text, sender text,
            original_run_id text);
        create table if not exists trust(
            vendor_id text primary key, points int default 0, confirms int default 0, overrides int default 0);
        create table if not exists vendor_emails(
            run_id text primary key, vendor_id text, recipient text, subject text not null, body text not null,
            status text not null, error text, attempted_at real not null, completed_at real);
        create table if not exists vendors(
            vendor_id text primary key, name text not null, email text not null default '');
        create table if not exists email_notifications(
            id integer primary key autoincrement, run_id text not null, vendor_id text,
            dedupe_key text not null unique, notification_key text not null,
            attempt_number integer not null default 1, resend_claimed_at real,
            recipient text, subject text not null, body text not null,
            status text not null check(status in ('SENT','FAILED','PENDING','OUTBOX','NOT_REQUIRED')),
            error text, created_at real not null, attempted_at real, completed_at real);
        create table if not exists processing_claims(
            file_hash text primary key, run_id text not null, created_at real not null);
        """)
        run_columns = {
            row["name"] for row in c.execute("pragma table_info(runs)").fetchall()
        }
        if "sender" not in run_columns:
            c.execute("alter table runs add column sender text")
        if "original_run_id" not in run_columns:
            c.execute("alter table runs add column original_run_id text")
        columns = {
            row["name"] for row in
            c.execute("pragma table_info(email_notifications)").fetchall()
        }
        migrations = (
            ("notification_key", "text"),
            ("attempt_number", "integer not null default 1"),
            ("resend_claimed_at", "real"),
        )
        for column, definition in migrations:
            if column not in columns:
                c.execute(f"alter table email_notifications add column {column} {definition}")
        c.execute(
            "update email_notifications set notification_key=dedupe_key "
            "where notification_key is null or notification_key=''"
        )
        table_sql = c.execute(
            "select sql from sqlite_master where type='table' and name='email_notifications'"
        ).fetchone()[0]
        if "CHECK(STATUS IN" in table_sql.upper() and "'OUTBOX'" not in table_sql.upper():
            for index in (
                "email_notifications_run_idx",
                "email_notifications_key_idx",
                "email_notifications_status_idx",
            ):
                c.execute(f"drop index if exists {index}")
            c.execute("alter table email_notifications rename to email_notifications_legacy")
            c.execute("""
                create table email_notifications(
                    id integer primary key autoincrement, run_id text not null, vendor_id text,
                    dedupe_key text not null unique, notification_key text not null,
                    attempt_number integer not null default 1, resend_claimed_at real,
                    recipient text, subject text not null, body text not null,
                    status text not null check(status in ('SENT','FAILED','PENDING','OUTBOX','NOT_REQUIRED')),
                    error text, created_at real not null, attempted_at real, completed_at real)
            """)
            c.execute("""
                insert into email_notifications
                select id,run_id,vendor_id,dedupe_key,notification_key,attempt_number,
                    resend_claimed_at,recipient,subject,body,status,error,created_at,
                    attempted_at,completed_at
                from email_notifications_legacy
            """)
            c.execute("drop table email_notifications_legacy")
        c.execute(
            "create index if not exists email_notifications_run_idx "
            "on email_notifications(run_id, id)"
        )
        c.execute(
            "create index if not exists email_notifications_key_idx "
            "on email_notifications(notification_key, id)"
        )
        c.execute(
            "create index if not exists email_notifications_status_idx "
            "on email_notifications(status, created_at)"
        )
        # Backfill known vendors and legacy .env contacts without overwriting
        # contacts already saved in the database.
        configured_emails = json.loads(os.getenv("VENDOR_EMAILS") or "{}")
        if not isinstance(configured_emails, dict):
            raise ValueError("VENDOR_EMAILS must be a JSON object keyed by vendor ID")
        for vendor_id, vendor in VENDORS.items():
            email = vendor.get("email") or configured_emails.get(vendor_id) or ""
            c.execute(
                "insert into vendors(vendor_id,name,email) values(?,?,?) "
                "on conflict(vendor_id) do update set name=excluded.name, "
                "email=case when vendors.email='' then excluded.email else vendors.email end",
                (vendor_id, vendor["name"], email),
            )
        # Migrate the original one-row-per-run email records to append-only
        # notification history. Do not remove the legacy rows.
        c.execute("""
            insert or ignore into email_notifications
                (run_id,vendor_id,dedupe_key,notification_key,recipient,subject,body,status,error,
                 created_at,attempted_at,completed_at)
            select run_id,vendor_id,'legacy:' || run_id,'legacy:' || run_id,recipient,subject,body,
                case lower(status)
                    when 'sent' then 'SENT'
                    when 'failed' then 'FAILED'
                    when 'outbox' then 'OUTBOX'
                    when 'not_required' then 'NOT_REQUIRED'
                    else 'PENDING'
                end,
                error,coalesce(attempted_at,0),attempted_at,completed_at
            from vendor_emails
        """)
        c.execute("delete from processing_claims where created_at < ?", (time.time() - 21600,))
