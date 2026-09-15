"""SQLite 持久化层。

设计要点
--------
- WAL 模式 + 每个工作连接开启 ``foreign_keys``，重启后数据保持一致。
- 所有多步写入都走 :meth:`Database.transaction`（BEGIN IMMEDIATE），
  任一断言/异常都会整体回滚，杜绝半成品状态。
- 用部分唯一索引在数据库层强约束关键业务不变量：
  * 同一报名 + 技能，最多一份「进行中」的考核（含复评链头）。
  * 同一学员 + 技能，最多一份有效资格（active/suspended），撤销与过期可重新发证。
  * 同一工位同时只有一个在岗派工；同一学员同时只有一份在岗派工。
  * 同一名考评员对同一考核只能打一次分。
"""
from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin','examiner','student')),
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skills (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    key_process   INTEGER NOT NULL DEFAULT 0,   -- 是否为关键工序技能
    min_consecutive INTEGER NOT NULL DEFAULT 2, -- 关键工序要求的连续合格次数
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS courses (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    skill_id      TEXT NOT NULL REFERENCES skills(id),
    required_hours     INTEGER NOT NULL DEFAULT 0,  -- 达标所需累计课时
    required_practice  INTEGER NOT NULL DEFAULT 0,  -- 达标所需合格实操
    allowed_absences   INTEGER NOT NULL DEFAULT 0,  -- 允许缺课次数上限
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workstations (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    skill_id      TEXT REFERENCES skills(id),       -- 关键工序绑定所需技能
    is_key        INTEGER NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS enrollments (
    id            TEXT PRIMARY KEY,
    student_id    TEXT NOT NULL REFERENCES users(id),
    course_id     TEXT NOT NULL REFERENCES courses(id),
    stage         TEXT NOT NULL DEFAULT 'enrolled'
                  CHECK (stage IN ('enrolled','learning','assessment','certified')),
    -- 累计课时、合格实操、缺课次数由服务层维护
    hours         INTEGER NOT NULL DEFAULT 0,
    practice_ok   INTEGER NOT NULL DEFAULT 0,
    absences      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    UNIQUE (student_id, course_id)
);

CREATE TABLE IF NOT EXISTS attendance (
    id            TEXT PRIMARY KEY,
    enrollment_id TEXT NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
    lesson_date   TEXT NOT NULL,
    present       INTEGER NOT NULL,
    hours         INTEGER NOT NULL DEFAULT 0,       -- 本次计入课时
    note          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS practicals (
    id            TEXT PRIMARY KEY,
    enrollment_id TEXT NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    passed        INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assessments (
    id            TEXT PRIMARY KEY,
    enrollment_id TEXT NOT NULL REFERENCES enrollments(id),
    skill_id      TEXT NOT NULL REFERENCES skills(id),
    kind          TEXT NOT NULL CHECK (kind IN ('initial','retake','review')),
    round         INTEGER NOT NULL DEFAULT 1,
    parent_id     TEXT REFERENCES assessments(id),
    status        TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open','in_review','passed','failed')),
    result_locked INTEGER NOT NULL DEFAULT 0,       -- 终态(passed/failed)=1
    pass_score    INTEGER NOT NULL DEFAULT 60,
    gap_threshold INTEGER NOT NULL DEFAULT 15,      -- 分差超过该值进入复评
    examiner_required INTEGER NOT NULL DEFAULT 2,   -- 至少考评员数量
    created_at    TEXT NOT NULL,
    finalized_at  TEXT
);
-- 同一报名+技能最多一份「未终态」的考核链头（普通考核或复评，parent 为空时是链头）。
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_chain
    ON assessments(enrollment_id, skill_id)
    WHERE result_locked = 0 AND parent_id IS NULL;

CREATE TABLE IF NOT EXISTS assessment_scores (
    id            TEXT PRIMARY KEY,
    assessment_id TEXT NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
    examiner_id   TEXT NOT NULL REFERENCES users(id),
    score         INTEGER NOT NULL CHECK (score >= 0 AND score <= 100),
    comment       TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    UNIQUE (assessment_id, examiner_id)            -- 同一考评员不得重复评分
);

CREATE TABLE IF NOT EXISTS qualifications (
    id            TEXT PRIMARY KEY,
    student_id    TEXT NOT NULL REFERENCES users(id),
    skill_id      TEXT NOT NULL REFERENCES skills(id),
    assessment_id TEXT NOT NULL UNIQUE REFERENCES assessments(id),
    cert_no       TEXT NOT NULL UNIQUE,
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','suspended','revoked','expired')),
    issued_at     TEXT NOT NULL,
    valid_until   TEXT NOT NULL,
    consecutive_passes INTEGER NOT NULL DEFAULT 0,
    supersedes_id TEXT REFERENCES qualifications(id)
);
-- 同一学员+技能最多一份「有效资格」（active 或 suspended）。
CREATE UNIQUE INDEX IF NOT EXISTS uq_valid_qual
    ON qualifications(student_id, skill_id)
    WHERE status IN ('active','suspended');

CREATE TABLE IF NOT EXISTS assignments (
    id            TEXT PRIMARY KEY,
    student_id    TEXT NOT NULL REFERENCES users(id),
    workstation_id TEXT NOT NULL REFERENCES workstations(id),
    qualification_id TEXT REFERENCES qualifications(id),
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','ended')),
    end_reason    TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    ended_at      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_station_active
    ON assignments(workstation_id) WHERE status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS uq_student_active
    ON assignments(student_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id      TEXT,
    action        TEXT NOT NULL,
    entity        TEXT NOT NULL,
    entity_id     TEXT,
    detail        TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency (
    idem_key      TEXT PRIMARY KEY,
    user_id       TEXT,
    method        TEXT NOT NULL,
    path          TEXT NOT NULL,
    status_code   INTEGER NOT NULL,
    body          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
"""


class Database:
    """封装 SQLite 连接与事务。测试用 ``:memory:``，生产用文件 + WAL。"""

    def __init__(self, path: str | Path = "data/app.db"):
        self.path = str(path)
        self.memory = self.path == ":memory:"
        if self.memory:
            # 共享缓存内存库：多条连接看到同一份数据（测试用），常驻一条连接保活。
            self.dsn = f"file:appmem_{uuid.uuid4().hex}?mode=memory&cache=shared"
            self.uri = True
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.dsn = self.path
            self.uri = False
        self._check = sqlite3.connect(
            self.dsn, check_same_thread=False, isolation_level=None, uri=self.uri,
        )
        self._apply_pragmas(self._check)
        self._check.executescript(SCHEMA)
        self._check.commit()

    @staticmethod
    def _apply_pragmas(conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if mode not in ("memory", "wal"):
            # 文件库开启 WAL 以保证并发读 + 重启一致；内存库无法开 WAL。
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")

    def connect(self) -> sqlite3.Connection:
        """每次请求取一条独立连接（WAL 下可并发读）。"""
        conn = sqlite3.connect(self.dsn, check_same_thread=False,
                               isolation_level=None, uri=self.uri)
        self._apply_pragmas(conn)
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE：进入即拿写锁，杜绝并发评定的写写竞态。

        任何异常都会 ROLLBACK；正常退出 COMMIT。
        """
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def close(self) -> None:
        try:
            self._check.close()
        except Exception:
            pass
