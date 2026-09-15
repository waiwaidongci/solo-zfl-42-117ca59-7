"""HTTP 层：ThreadingHTTPServer + 纯标准库路由。

- 鉴权：前端以 ``X-User-Id`` 指定当前登录用户，角色由服务层强制校验。
- 幂等：写请求可带 ``Idempotency-Key``；网络重试/重复提交时返回同一结果，
  且并发的相同键只会有一个真正落库（先占位、后写结果）。
- 业务异常映射为统一 JSON 错误体；未知的唯一约束冲突映射为 409（已回滚）。
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .db import Database
from .seed import seed
from .services import (
    Conflict,
    Forbidden,
    NotFound,
    Service,
    ServiceError,
    Unprocessable,
)
from .util import now_iso

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


class AppState:
    def __init__(self, db: Database):
        self.db = db
        self.service = Service(db)


def _json_default(obj):
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


class Handler(BaseHTTPRequestHandler):
    server_version = "LacquerThread/1.0"
    state: AppState = None  # 由 make_server 注入到类属性

    # 静默标准访问日志，统一走我们自己的输出格式。
    def log_message(self, fmt, *args):  # noqa: D401
        pass

    # ------------------------------------------------------------- 基础收发
    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status: int = 204) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _error(self, exc: ServiceError) -> None:
        payload = {"error": {"code": exc.code, "message": exc.message}}
        if isinstance(exc, Unprocessable) and exc.details:
            payload["error"]["details"] = exc.details
        self._send_json(exc.status, payload)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ServiceError("请求体不是合法 JSON", 400, "invalid_json")
        if not isinstance(data, dict):
            raise ServiceError("请求体必须是 JSON 对象", 400, "invalid_json")
        return data

    @property
    def actor_id(self) -> str | None:
        return self.headers.get("X-User-Id") or None

    # ------------------------------------------------------------- 路由
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/health":
                return self._send_json(200, {"ok": True, "time": now_iso()})
            if path.startswith("/api/"):
                return self._api_get(path)
            return self._serve_static(path)
        except ServiceError as e:
            return self._error(e)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            return self._send_json(404, {"error": {"code": "not_found", "message": "未知路径"}})
        try:
            data = self._read_json()
        except ServiceError as e:
            return self._error(e)

        idem_key = self.headers.get("Idempotency-Key")
        if idem_key:
            return self._with_idempotency(idem_key, "POST", path, data)
        self._dispatch_write("POST", path, data)

    # ----------------------------------------------------------- 幂等中间件
    def _with_idempotency(self, key: str, method: str, path: str, data: dict) -> None:
        db = self.state.db
        user = self.actor_id or ""
        # 1) 已完成？直接回放。
        done = self._idem_lookup(key)
        if done is not None:
            if done["user_id"] != user or done["path"] != path or done["method"] != method:
                return self._send_json(409, {"error": {
                    "code": "idempotency_conflict",
                    "message": "幂等键已用于其他请求"}})
            if done["status_code"]:
                return self._send_json(done["status_code"], json.loads(done["body"]))
            # 仍在处理中（并发重试）：等待对端写完。
            waited = 0.0
            while waited < 5.0:
                time.sleep(0.02)
                waited += 0.02
                done = self._idem_lookup(key)
                if done and done["status_code"]:
                    return self._send_json(done["status_code"], json.loads(done["body"]))
            return self._send_json(409, {"error": {
                "code": "request_in_progress", "message": "相同请求仍在处理中"}})

        # 2) 占位（唯一键，并发下只有一个成功）。
        try:
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO idempotency(idem_key,user_id,method,path,status_code,body,"
                    "created_at) VALUES (?,?,?,?,0,'',?)",
                    (key, user, method, path, now_iso()),
                )
        except sqlite3.IntegrityError:
            # 对端刚占位：退避后读取。
            time.sleep(0.01)
            return self._with_idempotency(key, method, path, data)

        # 3) 执行业务并落结果；任何结果（含 4xx）都缓存，5xx 删除占位以便重试。
        try:
            status, payload = self._execute_write("POST", path, data)
        except ServiceError as e:
            body = {"error": {"code": e.code, "message": e.message}}
            if isinstance(e, Unprocessable) and e.details:
                body["error"]["details"] = e.details
            self._idem_finish(key, e.status, body)
            return self._send_json(e.status, body)
        except sqlite3.IntegrityError as e:
            # 占位之外的并发约束冲突：删除占位，返回 409 以便客户端安全重试。
            self._idem_delete(key)
            return self._send_json(409, {"error": {
                "code": "concurrent_conflict",
                "message": f"并发冲突，事务已回滚：{e}"}})
        except Exception:  # noqa: BLE001
            self._idem_delete(key)
            raise
        self._idem_finish(key, status, payload)
        self._send_json(status, payload)

    def _idem_lookup(self, key: str):
        conn = self.state.db.connect()
        try:
            return conn.execute("SELECT * FROM idempotency WHERE idem_key=?", (key,)).fetchone()
        finally:
            conn.close()

    def _idem_finish(self, key: str, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default)
        with self.state.db.transaction() as conn:
            conn.execute("UPDATE idempotency SET status_code=?,body=? WHERE idem_key=?",
                         (status, body, key))

    def _idem_delete(self, key: str) -> None:
        try:
            with self.state.db.transaction() as conn:
                conn.execute("DELETE FROM idempotency WHERE idem_key=? AND status_code=0", (key,))
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------- GET 分发
    def _api_get(self, path: str) -> None:
        svc = self.state.service
        m = re.fullmatch(r"/api/enrollments/([^/]+)", path)
        if m:
            return self._send_json(200, svc.get_enrollment(m.group(1)))
        m = re.fullmatch(r"/api/assessments/([^/]+)", path)
        if m:
            return self._send_json(200, svc.get_assessment(m.group(1)))
        m = re.fullmatch(r"/api/qualifications/([^/]+)", path)
        if m:
            return self._send_json(200, svc.get_qualification(m.group(1)))
        simple = {
            "/api/users": "users",
            "/api/skills": "skills",
            "/api/courses": "courses",
            "/api/workstations": "workstations",
            "/api/enrollments": "enrollments",
            "/api/assessments": "assessments",
            "/api/qualifications": "qualifications",
            "/api/assignments": "assignments",
            "/api/audit": "audit_log",
        }
        if path in simple:
            return self._send_json(200, svc.list(simple[path]))
        raise NotFound("未知接口")

    # ------------------------------------------------------------- POST 分发
    def _dispatch_write(self, method: str, path: str, data: dict) -> None:
        try:
            status, payload = self._execute_write(method, path, data)
        except ServiceError as e:
            return self._error(e)
        except sqlite3.IntegrityError as e:
            return self._send_json(409, {"error": {
                "code": "concurrent_conflict",
                "message": f"并发/唯一约束冲突，事务已回滚：{e}"}})
        if payload is None:
            return self._send_empty(status)
        return self._send_json(status, payload)

    def _execute_write(self, method: str, path: str, data: dict):
        """返回 (status, payload)；抛 ServiceError / IntegrityError 由上层处理。"""
        svc = self.state.service
        actor = self.actor_id
        g = data.get

        # ---- 基础档案
        if path == "/api/users":
            return 201, svc.create_user(actor, g("name"), g("role"))
        if path == "/api/skills":
            return 201, svc.create_skill(actor, g("name"), bool(g("key_process", False)),
                                        int(g("min_consecutive", 2)))
        if path == "/api/courses":
            return 201, svc.create_course(
                actor, g("name"), g("skill_id"), g("required_hours", 0),
                g("required_practice", 0), g("allowed_absences", 0))
        if path == "/api/workstations":
            return 201, svc.create_workstation(
                actor, g("name"), g("skill_id") or None, bool(g("is_key", False)))

        # ---- 报名与学习
        if path == "/api/enrollments":
            return 201, svc.create_enrollment(actor, g("student_id"), g("course_id"))
        m = re.fullmatch(r"/api/enrollments/([^/]+)/start", path)
        if m:
            return 200, svc.start_learning(actor, m.group(1))
        m = re.fullmatch(r"/api/enrollments/([^/]+)/attendance", path)
        if m:
            return 201, svc.add_attendance(
                actor, m.group(1), g("lesson_date"), bool(g("present", True)),
                g("hours", 0), g("note", ""))
        m = re.fullmatch(r"/api/enrollments/([^/]+)/practicals", path)
        if m:
            return 201, svc.add_practice(actor, m.group(1), g("title"), bool(g("passed", True)))
        m = re.fullmatch(r"/api/enrollments/([^/]+)/advance", path)
        if m:
            return 200, svc.advance_to_assessment(actor, m.group(1))

        # ---- 考核与复评
        if path == "/api/assessments":
            return 201, svc.open_assessment(
                actor, g("enrollment_id"), g("kind", "initial"),
                g("pass_score", 60), g("gap_threshold", 15), g("examiner_required", 2))
        m = re.fullmatch(r"/api/assessments/([^/]+)/scores", path)
        if m:
            return 201, svc.add_score(actor, m.group(1), g("examiner_id"),
                                     g("score"), g("comment", ""))
        m = re.fullmatch(r"/api/assessments/([^/]+)/finalize", path)
        if m:
            return 200, svc.finalize_assessment(actor, m.group(1))
        m = re.fullmatch(r"/api/assessments/([^/]+)/review", path)
        if m:
            return 201, svc.schedule_review(actor, m.group(1))

        # ---- 发证与资格管理
        if path == "/api/certificates":
            return 201, svc.issue_certificate(actor, g("enrollment_id"),
                                             g("valid_months", 12))
        m = re.fullmatch(r"/api/qualifications/([^/]+)/suspend", path)
        if m:
            return 200, svc.suspend_qualification(actor, m.group(1), g("reason", ""))
        m = re.fullmatch(r"/api/qualifications/([^/]+)/revoke", path)
        if m:
            return 200, svc.revoke_qualification(actor, m.group(1), g("reason", ""))
        m = re.fullmatch(r"/api/qualifications/([^/]+)/restore", path)
        if m:
            return 200, svc.restore_qualification(actor, m.group(1))

        # ---- 派工
        if path == "/api/assignments":
            return 201, svc.assign_workstation(actor, g("student_id"), g("workstation_id"))
        m = re.fullmatch(r"/api/assignments/([^/]+)/end", path)
        if m:
            return 200, svc.end_assignment(actor, m.group(1), g("reason", ""))

        # ---- 演练辅助：把某资格的有效期改到过去（等价于真实到期），用于演示到期停派工
        m = re.fullmatch(r"/api/dev/qualifications/([^/]+)/expire", path)
        if m:
            return 200, svc.dev_mark_expired(actor, m.group(1))

        raise NotFound("未知接口")

    # ------------------------------------------------------------- 静态文件
    def _serve_static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/index.html"
        # 防目录穿越：规范化后必须仍位于 WEB_ROOT 内。
        target = (WEB_ROOT / path.lstrip("/")).resolve()
        try:
            target.relative_to(WEB_ROOT.resolve())
        except ValueError:
            raise NotFound("文件不存在")
        if not target.is_file():
            raise NotFound("文件不存在")
        body = target.read_bytes()
        ctype = _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(host: str, port: int, db: Database, run_seed: bool = True) -> ThreadingHTTPServer:
    if run_seed:
        seed(db)
    # 每次启动（含 --no-seed 对已有库重启）都清扫到期资格并停止其派工，
    # 保证“停机期间到期 → 重启即生效”。
    n = Service(db).expire_qualifications()
    if n:
        print(f"[startup] 已将 {n} 份到期资格置为 expired 并停止其派工")
    state = AppState(db)

    class _Handler(Handler):
        pass

    _Handler.state = state
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.daemon_threads = True
    httpd.state = state
    return httpd
