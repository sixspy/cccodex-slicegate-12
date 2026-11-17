from datetime import datetime
import hashlib, secrets, sqlite3
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

# 订阅规则簿锁定口径（见 docs/订阅规则簿.md）。互斥口径择一后禁止改口：
# 选定「甲」：订阅变更与放行可见集同一库事务内提交，失败整笔回滚。
SEMANTICS = "甲：订阅变更与放行可见集同一库事务内提交，失败整笔回滚"


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))


def _add_column(conn, table: str, col: str, ddl: str):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def check_semantics(conn):
    """订阅规则簿已落库的口径禁止改口；偏离即拒绝。"""
    row = conn.execute("SELECT semantics FROM subscription_meta WHERE id=1").fetchone()
    if not row or row["semantics"] != SEMANTICS:
        raise HTTPException(409, "订阅规则簿已锁定口径「甲」，禁止改口")


class LoginIn(BaseModel):
    username: str
    password: str


def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u


from typing import Any
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "日切放行"
SKELETON = "业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS slices(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_no TEXT UNIQUE NOT NULL,
          biz_date TEXT NOT NULL,
          partition_key TEXT NOT NULL,
          row_count INTEGER NOT NULL,
          expected_count INTEGER NOT NULL,
          completeness TEXT NOT NULL,
          release_status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS releases(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_id INTEGER NOT NULL,
          consumer TEXT NOT NULL,
          status TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS watermarks(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          consumer TEXT NOT NULL,
          biz_date TEXT NOT NULL,
          watermark TEXT NOT NULL,
          source_slice TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS subscriptions(
          consumer TEXT PRIMARY KEY,
          status TEXT NOT NULL,
          updated_by TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          version INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS subscription_meta(
          id INTEGER PRIMARY KEY CHECK(id=1),
          version INTEGER NOT NULL,
          semantics TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS subscription_audit(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version INTEGER NOT NULL,
          consumer TEXT NOT NULL,
          action TEXT NOT NULL,
          actor TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        # 放行详情记口径版本、水位记关联放行，四画面同源对读
        _add_column(conn, "releases", "sub_version", "sub_version INTEGER")
        _add_column(conn, "watermarks", "release_id", "release_id INTEGER")
        if conn.execute("SELECT COUNT(*) c FROM subscription_meta").fetchone()["c"] == 0:
            conn.execute("INSERT INTO subscription_meta(id,version,semantics) VALUES (1,0,?)", (SEMANTICS,))
        check_semantics(conn)
        if conn.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"] == 0:
            rows = [
                ("SL-2026-11-01-east", "2026-11-01", "east", 980, 1000, "部分完整", "未放行"),
                ("SL-2026-11-01-west", "2026-11-01", "west", 1000, 1000, "完整", "已放行"),
                ("SL-2026-11-02-east", "2026-11-02", "east", 0, 1000, "待检", "未放行"),
            ]
            for r in rows:
                conn.execute("INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,completeness,release_status) VALUES (?,?,?,?,?,?,?)", r)
            conn.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at,sub_version) VALUES (2,'ads_order','成功','完整切片放行','2026-11-01T18:00:00',1)")
            conn.execute("INSERT INTO watermarks(consumer,biz_date,watermark,source_slice,release_id) VALUES ('ads_order','2026-11-01','2026-11-01T18:00:00','SL-2026-11-01-west',1)")
        if conn.execute("SELECT COUNT(*) c FROM subscriptions").fetchone()["c"] == 0:
            ts = now_iso()
            seed = [("ads_order", "订阅中"), ("risk_bi", "订阅中"), ("legacy_bi", "已退订")]
            for consumer, status in seed:
                conn.execute("INSERT INTO subscriptions(consumer,status,updated_by,updated_at,version) VALUES (?,?,?,?,1)",
                             (consumer, status, "system", ts))
                conn.execute("INSERT INTO subscription_audit(version,consumer,action,actor,note,created_at) VALUES (1,?,?,?,?,?)",
                             (consumer, "初始登记", "system", f"初始{status}，口径版本v1", ts))
            conn.execute("UPDATE subscription_meta SET version=1 WHERE id=1")


@app.on_event("startup")
def _start():
    init_db()


@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"]
        meta = c.execute("SELECT version, semantics FROM subscription_meta WHERE id=1").fetchone()
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "slices": n, "db": str(DB_PATH),
            "sub_version": meta["version"], "semantics": meta["semantics"]}


@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username, role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row:
        raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok] = row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}


@app.get("/api/slices")
def list_slices(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM slices ORDER BY id").fetchall()
    return {"items": [dict(r) for r in rows]}


class SliceIn(BaseModel):
    slice_no: str
    biz_date: str
    partition_key: str
    row_count: int
    expected_count: int


@app.post("/api/slices")
def add_slice(body: SliceIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    comp = "完整" if body.row_count >= body.expected_count and body.expected_count > 0 else ("部分完整" if body.row_count > 0 else "待检")
    with connect() as c:
        try:
            cur = c.execute(
                "INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,completeness,release_status) VALUES (?,?,?,?,?,?,?)",
                (body.slice_no, body.biz_date, body.partition_key, body.row_count, body.expected_count, comp, "未放行"),
            )
            return {"id": cur.lastrowid, "completeness": comp}
        except sqlite3.IntegrityError:
            raise HTTPException(409, "切片号已存在")


@app.post("/api/slices/{sid}/evaluate")
def evaluate(sid: int, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        comp = "完整" if row["row_count"] >= row["expected_count"] and row["expected_count"] > 0 else ("部分完整" if row["row_count"] > 0 else "待检")
        c.execute("UPDATE slices SET completeness=? WHERE id=?", (comp, sid))
    return {"id": sid, "completeness": comp}


# ---------- 消费方订阅清单（口径甲：与放行可见集同一事务，同成同败） ----------

@app.get("/api/subscriptions")
def list_subscriptions(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        check_semantics(c)
        rows = c.execute("SELECT * FROM subscriptions ORDER BY consumer").fetchall()
        meta = c.execute("SELECT version, semantics FROM subscription_meta WHERE id=1").fetchone()
    return {"items": [dict(r) for r in rows], "version": meta["version"], "semantics": meta["semantics"]}


@app.get("/api/subscriptions/audit")
def list_subscription_audit(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM subscription_audit ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}


class SubscriptionIn(BaseModel):
    consumer: str
    action: str  # 订阅 / 退订
    expected_version: int


@app.post("/api/subscriptions")
def change_subscription(body: SubscriptionIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    consumer = body.consumer.strip()
    if not consumer:
        raise HTTPException(400, "消费方不能为空")
    if body.action not in ("订阅", "退订"):
        raise HTTPException(400, "动作仅支持 订阅/退订")
    ts = now_iso()
    # 两名订阅值班同时改落：BEGIN IMMEDIATE 写锁串行 + 口径版本乐观校验，
    # 后手写明「订阅争用」并落审计，禁止默成。
    conn = connect()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        check_semantics(conn)
        meta = conn.execute("SELECT version FROM subscription_meta WHERE id=1").fetchone()
        if meta["version"] != body.expected_version:
            cur_version = meta["version"]
            conn.execute("ROLLBACK")
            with connect() as audit_conn:
                audit_conn.execute(
                    "INSERT INTO subscription_audit(version,consumer,action,actor,note,created_at) VALUES (?,?,?,?,?,?)",
                    (cur_version, consumer, "订阅争用", user,
                     f"期望口径版本v{body.expected_version}，当前v{cur_version}，后手禁止默成", ts))
            raise HTTPException(409, f"订阅争用：清单已被他人改至口径版本v{cur_version}，请刷新后重试")
        new_status = "订阅中" if body.action == "订阅" else "已退订"
        new_version = meta["version"] + 1
        # 甲口径：订阅变更与放行可见集（口径版本）同一事务提交，失败整笔回滚
        conn.execute(
            "INSERT INTO subscriptions(consumer,status,updated_by,updated_at,version) VALUES (?,?,?,?,?) "
            "ON CONFLICT(consumer) DO UPDATE SET status=excluded.status, updated_by=excluded.updated_by, "
            "updated_at=excluded.updated_at, version=excluded.version",
            (consumer, new_status, user, ts, new_version))
        conn.execute("UPDATE subscription_meta SET version=? WHERE id=1", (new_version,))
        conn.execute(
            "INSERT INTO subscription_audit(version,consumer,action,actor,note,created_at) VALUES (?,?,?,?,?,?)",
            (new_version, consumer, body.action, user, f"{body.action}生效，放行可见集同步至口径版本v{new_version}", ts))
        conn.execute("COMMIT")
    except HTTPException:
        raise
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()
    return {"ok": True, "consumer": consumer, "status": new_status, "version": new_version}


class ReleaseIn(BaseModel):
    consumer: str


@app.post("/api/slices/{sid}/release")
def release(sid: int, body: ReleaseIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    ts = now_iso()
    with connect() as c:
        check_semantics(c)
        row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        meta = c.execute("SELECT version FROM subscription_meta WHERE id=1").fetchone()
        if row["completeness"] != "完整":
            c.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at,sub_version) VALUES (?,?,?,?,?,?)",
                      (sid, body.consumer, "拒绝", "完整度未达标", ts, meta["version"]))
            c.commit()
            raise HTTPException(409, "未完整禁止放行")
        sub = c.execute("SELECT status FROM subscriptions WHERE consumer=? AND status='订阅中'", (body.consumer,)).fetchone()
        if not sub:
            # 偏离订阅规则簿：已删/未订阅消费方禁止偷接放行、禁止推水位，拒绝并记下「订阅口径偏了」
            note = f"订阅口径偏了：消费方 {body.consumer} 未在订阅清单（口径版本v{meta['version']}）"
            c.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at,sub_version) VALUES (?,?,?,?,?,?)",
                      (sid, body.consumer, "拒绝", note, ts, meta["version"]))
            c.execute("INSERT INTO subscription_audit(version,consumer,action,actor,note,created_at) VALUES (?,?,?,?,?,?)",
                      (meta["version"], body.consumer, "口径偏离拒绝", user, note, ts))
            c.commit()
            raise HTTPException(409, note)
        # 甲口径同事务：切片放行态 + 放行流水(记口径版本) + 水位(记关联放行) 同成同败
        c.execute("UPDATE slices SET release_status='已放行' WHERE id=?", (sid,))
        cur = c.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at,sub_version) VALUES (?,?,?,?,?,?)",
                        (sid, body.consumer, "成功", "完整切片放行", ts, meta["version"]))
        c.execute("INSERT INTO watermarks(consumer,biz_date,watermark,source_slice,release_id) VALUES (?,?,?,?,?)",
                  (body.consumer, row["biz_date"], ts, row["slice_no"], cur.lastrowid))
    return {"ok": True, "sub_version": meta["version"]}


@app.get("/api/releases")
def list_releases(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""SELECT r.*, s.slice_no FROM releases r JOIN slices s ON s.id=r.slice_id ORDER BY r.id DESC""").fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/watermarks")
def list_wm(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM watermarks ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
