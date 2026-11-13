import argparse
import getpass
import hashlib
import secrets
import sqlite3
import time
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


DB = Path(__file__).with_name("data") / "team.sqlite3"
TTL = 12 * 60 * 60


@contextmanager
def connect():
    db = sqlite3.connect(DB, timeout=15)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def initialize():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users(name TEXT PRIMARY KEY, salt BLOB, password BLOB, admin INTEGER);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, name TEXT REFERENCES users, expires INTEGER);
        CREATE TABLE IF NOT EXISTS registration_codes(token TEXT PRIMARY KEY, answer TEXT NOT NULL, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS owners(doc TEXT PRIMARY KEY, name TEXT REFERENCES users);
        CREATE TABLE IF NOT EXISTS shares(doc TEXT, name TEXT REFERENCES users, PRIMARY KEY(doc,name));
        CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY, title TEXT NOT NULL, creator TEXT REFERENCES users, invite TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER REFERENCES groups, name TEXT REFERENCES users, PRIMARY KEY(group_id,name));
        CREATE TABLE IF NOT EXISTS group_files(group_id INTEGER REFERENCES groups, doc TEXT NOT NULL, PRIMARY KEY(group_id,doc));
        CREATE TABLE IF NOT EXISTS corrections(doc TEXT PRIMARY KEY, correction TEXT);
        CREATE TABLE IF NOT EXISTS revision_sets(doc TEXT PRIMARY KEY, payload TEXT NOT NULL, version TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS revision_events(id INTEGER PRIMARY KEY, doc TEXT, reviewer TEXT, at TEXT, payload TEXT, version TEXT);
        CREATE TABLE IF NOT EXISTS migrated(doc TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS confirmations(
            doc TEXT, scope TEXT, item TEXT, reviewer TEXT, at TEXT, PRIMARY KEY(doc,scope,item));
        CREATE TABLE IF NOT EXISTS confirmation_events(
            id INTEGER PRIMARY KEY, doc TEXT, scope TEXT, item TEXT, reviewer TEXT, at TEXT, confirmed INTEGER);
        """)


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)


def add_user(name, password, admin=False):
    if not name.strip() or len(name) > 40 or not 10 <= len(password) <= 128:
        raise ValueError("账号须为 1–40 个字符，密码须为 10–128 个字符")
    salt = secrets.token_bytes(16)
    with connect() as db:
        db.execute("INSERT INTO users VALUES (?,?,?,?)", (name, salt, password_hash(password, salt), admin))


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def issue_registration_code():
    token = secrets.token_urlsafe(24)
    code = "".join(secrets.choice("23456789ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(5))
    with connect() as db:
        now = int(time.time())
        db.execute("DELETE FROM registration_codes WHERE expires<=?", (now,))
        db.execute("INSERT INTO registration_codes VALUES (?,?,?)", (token, digest(token + code), now + 300))
    return token, code


def verify_registration_code(token, code):
    with connect() as db:
        row = db.execute("DELETE FROM registration_codes WHERE token=? RETURNING answer,expires", (token,)).fetchone()
    return bool(row and row["expires"] > time.time()
                and secrets.compare_digest(row["answer"], digest(token + code.strip().upper())))


def login(name, password):
    with connect() as db:
        row = db.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
        candidate = password_hash(password, row["salt"] if row else bytes(16))
        if not row or not secrets.compare_digest(candidate, row["password"]):
            return None
        token = secrets.token_urlsafe(32)
        db.execute("DELETE FROM sessions WHERE expires<=?", (int(time.time()),))
        db.execute("INSERT INTO sessions VALUES (?,?,?)", (digest(token), name, int(time.time()) + TTL))
    return token


def identity(token):
    if token:
        with connect() as db:
            row = db.execute("""SELECT users.name, admin FROM sessions JOIN users USING(name)
                              WHERE token=? AND expires>?""", (digest(token), int(time.time()))).fetchone()
            return dict(row) if row else None
    return None


def logout(token):
    with connect() as db:
        db.execute("DELETE FROM sessions WHERE token=?", (digest(token),))


def owns(user, doc):
    if user["admin"]:
        return True
    with connect() as db:
        return bool(db.execute("SELECT 1 FROM owners WHERE doc=? AND name=?", (doc, user["name"])).fetchone())


def can_access(user, doc):
    if owns(user, doc):
        return True
    with connect() as db:
        return bool(db.execute("""
            SELECT 1 FROM shares WHERE doc=? AND name=?
            UNION ALL
            SELECT 1 FROM group_files JOIN group_members USING(group_id) WHERE doc=? AND name=?
            """, (doc, user["name"], doc, user["name"])).fetchone())


def groups_for(name):
    with connect() as db:
        return [dict(row) for row in db.execute("""
            SELECT id, title, creator, CASE WHEN creator=? THEN invite END AS invite
            FROM groups JOIN group_members ON id=group_id WHERE name=? ORDER BY title, id
            """, (name, name))]


def create_group(name, title):
    with connect() as db:
        group = db.execute("INSERT INTO groups(title,creator,invite) VALUES (?,?,?)",
                           (title, name, secrets.token_urlsafe(18))).lastrowid
        db.execute("INSERT INTO group_members VALUES (?,?)", (group, name))


def join_group(name, invite):
    with connect() as db:
        row = db.execute("SELECT id FROM groups WHERE invite=?", (invite,)).fetchone()
        if not row:
            return False
        db.execute("INSERT OR IGNORE INTO group_members VALUES (?,?)", (row["id"], name))
        return True


def leave_group(name, group):
    with connect() as db:
        return db.execute("""DELETE FROM group_members WHERE group_id=? AND name=?
                          AND NOT EXISTS (SELECT 1 FROM groups WHERE id=? AND creator=?)""",
                          (group, name, group, name)).rowcount > 0


def document_groups(doc, name, group=None, grant=True):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if group is not None:
            if grant:
                if not db.execute("SELECT 1 FROM group_members WHERE group_id=? AND name=?", (group, name)).fetchone():
                    raise ValueError("请先加入该群组")
                db.execute("INSERT OR IGNORE INTO group_files VALUES (?,?)", (group, doc))
            else:
                db.execute("DELETE FROM group_files WHERE group_id=? AND doc=?", (group, doc))
        return [dict(row) for row in db.execute(
            "SELECT id,title FROM groups JOIN group_files ON id=group_id WHERE doc=? ORDER BY title", (doc,))]


def own(doc, name):
    with connect() as db:
        db.execute("INSERT INTO owners VALUES (?,?)", (doc, name))


def shares(doc, name=None, grant=True):
    with connect() as db:
        if name is not None:
            if grant:
                if not db.execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone():
                    raise ValueError("该团队账号不存在")
                db.execute("INSERT OR IGNORE INTO shares VALUES (?,?)", (doc, name))
            else:
                db.execute("DELETE FROM shares WHERE doc=? AND name=?", (doc, name))
        return [row[0] for row in db.execute("SELECT name FROM shares WHERE doc=? ORDER BY name", (doc,))]


def correction(doc, target=None):
    with connect() as db:
        if target is not None:
            if target:
                db.execute("INSERT INTO corrections VALUES (?,?) ON CONFLICT(doc) DO UPDATE SET correction=excluded.correction", (doc, target))
            else:
                db.execute("DELETE FROM corrections WHERE doc=?", (doc,))
        row = db.execute("SELECT correction FROM corrections WHERE doc=?", (doc,)).fetchone()
        return row[0] if row else ""


def revisions(doc, entries=None, expected=None, reviewer='本地用户'):
    from revision_chain import fingerprint
    empty = {'entries': [], 'version': fingerprint([])}
    if entries is None and not DB.is_file():
        return empty
    initialize()
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT payload,version FROM revision_sets WHERE doc=?', (doc,)).fetchone()
        result = {'entries': json.loads(row['payload']), 'version': row['version']} if row else empty
        if entries is not None:
            if expected != result['version']:
                raise ValueError('公告列表已被其他人修改，请刷新后重试')
            payload, version = json.dumps(entries, ensure_ascii=False), fingerprint(entries)
            db.execute('INSERT INTO revision_sets VALUES (?,?,?) ON CONFLICT(doc) DO UPDATE SET payload=excluded.payload,version=excluded.version', (doc,payload,version))
            db.execute('INSERT INTO revision_events(doc,reviewer,at,payload,version) VALUES (?,?,?,?,?)',
                       (doc,reviewer,datetime.now(timezone.utc).isoformat(),payload,version))
            result = {'entries': entries, 'version': version}
        return result


def confirmations(doc, legacy, change=None):
    """读取确认状态，可选写入一次变更。

    change 可以是单条 (scope, item, reviewer, confirmed)，
    也可以是批量 ((scope, item, reviewer, confirmed), ...)，批量在同一事务内完成。
    """
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        migrated = db.execute("INSERT OR IGNORE INTO migrated VALUES (?)", (doc,)).rowcount
        if migrated:
            for scope, items in legacy.items():
                if isinstance(items, dict):
                    db.executemany("INSERT OR IGNORE INTO confirmations VALUES (?,?,?,?,?)",
                                   [(doc, scope, item, meta.get("by") or "历史记录", meta.get("at") or "")
                                    for item, meta in items.items() if isinstance(meta, dict)])
        if change:
            changes = change if isinstance(change, (list, tuple)) and change and isinstance(change[0], (list, tuple)) else [change]
            at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for scope, item, reviewer, confirmed in changes:
                if confirmed:
                    db.execute("""INSERT INTO confirmations VALUES (?,?,?,?,?) ON CONFLICT(doc,scope,item)
                                  DO UPDATE SET reviewer=excluded.reviewer, at=excluded.at""",
                               (doc, scope, item, reviewer, at))
                else:
                    db.execute("DELETE FROM confirmations WHERE doc=? AND scope=? AND item=?", (doc, scope, item))
                db.execute("INSERT INTO confirmation_events(doc,scope,item,reviewer,at,confirmed) VALUES (?,?,?,?,?,?)",
                           (doc, scope, item, reviewer, at, confirmed))
        result = {}
        for row in db.execute("SELECT * FROM confirmations WHERE doc=?", (doc,)):
            result.setdefault(row["scope"], {})[row["item"]] = {"by": row["reviewer"], "at": row["at"]}
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("--admin", action="store_true")
    args = parser.parse_args()
    password = getpass.getpass("密码：")
    if password != getpass.getpass("再次输入密码："):
        parser.exit(1, "两次输入的密码不一致\n")
    initialize()
    try:
        add_user(args.name.strip(), password, args.admin)
    except (ValueError, sqlite3.IntegrityError) as error:
        parser.exit(1, f"创建账号失败：{error}\n")
