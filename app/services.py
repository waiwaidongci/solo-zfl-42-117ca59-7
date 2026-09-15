"""业务服务层：全部写操作都在单个 BEGIN IMMEDIATE 事务中完成。

关键不变量（门控 / 复评 / 唯一资格 / 派工）既在服务层判定，也由数据库
部分唯一索引兜底；任何一步失败都整体回滚。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from .db import Database
from .util import add_months_iso, now_iso

ROLE_ADMIN = "admin"
ROLE_EXAMINER = "examiner"
ROLE_STUDENT = "student"


class ServiceError(Exception):
    """带 HTTP 状态码与机器可读 code 的业务异常。"""

    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class NotFound(ServiceError):
    def __init__(self, message: str = "记录不存在"):
        super().__init__(message, 404, "not_found")


class Forbidden(ServiceError):
    def __init__(self, message: str = "无权执行该操作"):
        super().__init__(message, 403, "forbidden")


class Conflict(ServiceError):
    def __init__(self, message: str, code: str = "conflict"):
        super().__init__(message, 409, code)


class Unprocessable(ServiceError):
    def __init__(self, message: str, code: str = "gate_not_met", details: dict | None = None):
        super().__init__(message, 422, code)
        self.details = details or {}


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _rd(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Service:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ utils
    def _audit(
        self, conn: sqlite3.Connection, actor_id: str | None, action: str,
        entity: str, entity_id: str | None, detail: Any = None,
    ) -> None:
        conn.execute(
            "INSERT INTO audit_log(actor_id,action,entity,entity_id,detail,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (actor_id, action, entity, entity_id,
             json.dumps(detail, ensure_ascii=False) if detail is not None else "", now_iso()),
        )

    def _row(self, conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row:
        row = conn.execute(sql, params).fetchone()
        if row is None:
            raise NotFound()
        return row

    def _user(self, conn: sqlite3.Connection, user_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise Forbidden("身份不存在")
        if not row["active"]:
            raise Forbidden("账号已停用")
        return row

    def _require_role(self, conn: sqlite3.Connection, actor_id: str | None, *roles: str) -> sqlite3.Row:
        if not actor_id:
            raise Forbidden("缺少身份")
        user = self._user(conn, actor_id)
        if user["role"] not in roles:
            raise Forbidden(f"需要角色：{'/'.join(roles)}")
        return user

    # ------------------------------------------------------------- 基础档案
    def create_user(self, actor_id: str | None, name: str, role: str,
                    *, system: bool = False) -> dict:
        """创建人员。普通调用必须是管理员；system=True 仅供种子引导首个管理员。"""
        name = (name or "").strip()
        if role not in ("admin", "examiner", "student"):
            raise ServiceError("角色非法", 400, "invalid_role")
        if not name:
            raise ServiceError("姓名必填", 400, "invalid_name")
        with self.db.transaction() as conn:
            if not system:
                self._require_role(conn, actor_id, ROLE_ADMIN)
            uid = _new_id("U")
            conn.execute(
                "INSERT INTO users(id,name,role,active,created_at) VALUES (?,?,?,1,?)",
                (uid, name, role, now_iso()),
            )
            self._audit(conn, actor_id, "user.create", "user", uid, {"name": name, "role": role})
            return _rd(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())

    def create_skill(self, actor_id: str, name: str, key_process: bool = False,
                     min_consecutive: int = 2) -> dict:
        name = (name or "").strip()
        if not name:
            raise ServiceError("技能名称必填", 400)
        if int(min_consecutive) < 1:
            raise ServiceError("关键工序连续合格次数至少为 1", 400)
        with self.db.transaction() as conn:
            self._require_role(conn, actor_id, ROLE_ADMIN)
            sid = _new_id("S")
            conn.execute(
                "INSERT INTO skills(id,name,key_process,min_consecutive,created_at)"
                " VALUES (?,?,?,?,?)",
                (sid, name, 1 if key_process else 0, int(min_consecutive), now_iso()),
            )
            self._audit(conn, actor_id, "skill.create", "skill", sid, {"name": name})
            return _rd(conn.execute("SELECT * FROM skills WHERE id=?", (sid,)).fetchone())

    def create_course(self, actor_id: str, name: str, skill_id: str,
                      required_hours: int, required_practice: int,
                      allowed_absences: int) -> dict:
        name = (name or "").strip()
        if not name:
            raise ServiceError("课程名称必填", 400)
        try:
            required_hours, required_practice = int(required_hours), int(required_practice)
            allowed_absences = int(allowed_absences)
        except (TypeError, ValueError):
            raise ServiceError("课时/实操/缺课上限必须是整数", 400)
        if required_hours < 0 or required_practice < 0 or allowed_absences < 0:
            raise ServiceError("课时/实操/缺课上限不能为负", 400)
        with self.db.transaction() as conn:
            self._require_role(conn, actor_id, ROLE_ADMIN)
            self._row(conn, "SELECT id FROM skills WHERE id=?", (skill_id,))
            cid = _new_id("C")
            conn.execute(
                "INSERT INTO courses(id,name,skill_id,required_hours,required_practice,"
                "allowed_absences,created_at) VALUES (?,?,?,?,?,?,?)",
                (cid, name, skill_id, required_hours, required_practice,
                 allowed_absences, now_iso()),
            )
            self._audit(conn, actor_id, "course.create", "course", cid,
                        {"name": name, "skill_id": skill_id})
            return _rd(conn.execute("SELECT * FROM courses WHERE id=?", (cid,)).fetchone())

    def create_workstation(self, actor_id: str, name: str, skill_id: str | None,
                           is_key: bool) -> dict:
        name = (name or "").strip()
        if not name:
            raise ServiceError("工位名称必填", 400)
        with self.db.transaction() as conn:
            self._require_role(conn, actor_id, ROLE_ADMIN)
            if is_key and not skill_id:
                raise ServiceError("关键工序工位必须绑定技能", 400)
            if skill_id:
                skill = self._row(conn, "SELECT * FROM skills WHERE id=?", (skill_id,))
                if is_key and not skill["key_process"]:
                    raise ServiceError("被绑定技能未标记为关键工序技能", 400)
            wid = _new_id("W")
            conn.execute(
                "INSERT INTO workstations(id,name,skill_id,is_key,active,created_at)"
                " VALUES (?,?,?,?,1,?)",
                (wid, name, skill_id, 1 if is_key else 0, now_iso()),
            )
            self._audit(conn, actor_id, "workstation.create", "workstation", wid,
                        {"name": name, "is_key": bool(is_key)})
            return _rd(conn.execute("SELECT * FROM workstations WHERE id=?", (wid,)).fetchone())

    # ----------------------------------------------------------------- 报名
    def create_enrollment(self, actor_id: str, student_id: str, course_id: str) -> dict:
        with self.db.transaction() as conn:
            self._require_role(conn, actor_id, ROLE_ADMIN)
            student = self._user(conn, student_id)
            if student["role"] != ROLE_STUDENT:
                raise ServiceError("只能为学员报名", 400)
            self._row(conn, "SELECT id FROM courses WHERE id=?", (course_id,))
            dup = conn.execute(
                "SELECT id FROM enrollments WHERE student_id=? AND course_id=?",
                (student_id, course_id),
            ).fetchone()
            if dup:
                raise Conflict("该学员已报名本课程，不能重复报名", "duplicate_enrollment")
            eid = _new_id("E")
            conn.execute(
                "INSERT INTO enrollments(id,student_id,course_id,stage,created_at)"
                " VALUES (?,?,?,?,?)",
                (eid, student_id, course_id, "enrolled", now_iso()),
            )
            self._audit(conn, actor_id, "enrollment.create", "enrollment", eid,
                        {"student_id": student_id, "course_id": course_id})
            return self.get_enrollment(eid, conn=conn)

    def start_learning(self, actor_id: str, enrollment_id: str) -> dict:
        with self.db.transaction() as conn:
            self._require_role(conn, actor_id, ROLE_ADMIN)
            enr = self._row(conn, "SELECT * FROM enrollments WHERE id=?", (enrollment_id,))
            if enr["stage"] != "enrolled":
                raise Conflict(f"当前阶段为 {enr['stage']}，无需重复进入学习", "stage_invalid")
            conn.execute("UPDATE enrollments SET stage='learning' WHERE id=?", (enrollment_id,))
            self._audit(conn, actor_id, "enrollment.stage", "enrollment", enrollment_id,
                        {"to": "learning"})
            return self.get_enrollment(enrollment_id, conn=conn)

    def gate_status(self, enrollment_id: str, conn: sqlite3.Connection | None = None) -> dict:
        own = conn is None
        if own:
            conn = self.db.connect()
        try:
            enr = self._row(conn, "SELECT * FROM enrollments WHERE id=?", (enrollment_id,))
            course = self._row(conn, "SELECT * FROM courses WHERE id=?", (enr["course_id"],))
            checks = {
                "hours": {"actual": enr["hours"], "need": course["required_hours"],
                          "ok": enr["hours"] >= course["required_hours"]},
                "practice": {"actual": enr["practice_ok"], "need": course["required_practice"],
                             "ok": enr["practice_ok"] >= course["required_practice"]},
                "absences": {"actual": enr["absences"], "need": course["allowed_absences"],
                             # 缺课次数不得超过上限
                             "ok": enr["absences"] <= course["allowed_absences"]},
            }
            return {"ready": all(c["ok"] for c in checks.values()), "checks": checks}
        finally:
            if own:
                conn.close()

    def advance_to_assessment(self, actor_id: str, enrollment_id: str) -> dict:
        """课时 + 实操 + 缺课三门同时达标才允许进入考核阶段。"""
        with self.db.transaction() as conn:
            self._require_role(conn, actor_id, ROLE_ADMIN)
            enr = self._row(conn, "SELECT * FROM enrollments WHERE id=?", (enrollment_id,))
            if enr["stage"] in ("assessment", "certified"):
                raise Conflict("已进入考核阶段，不能重复推进", "stage_invalid")
            if enr["stage"] != "learning":
                raise Conflict("须先进入学习阶段", "stage_invalid")
            gate = self.gate_status(enrollment_id, conn=conn)
            if not gate["ready"]:
                raise Unprocessable("课时、实操或考勤未达标，不能进入考核",
                                    "gate_not_met", gate)
            conn.execute("UPDATE enrollments SET stage='assessment' WHERE id=?", (enrollment_id,))
            self._audit(conn, actor_id, "enrollment.stage", "enrollment", enrollment_id,
                        {"to": "assessment", "gate": gate["checks"]})
            return self.get_enrollment(enrollment_id, conn=conn)

    def add_attendance(self, actor_id: str, enrollment_id: str, lesson_date: str,
                       present: bool, hours: int = 0, note: str = "") -> dict:
        try:
            hours = int(hours)
        except (TypeError, ValueError):
            raise ServiceError("课时必须是整数", 400)
        if hours < 0:
            raise ServiceError("课时不能为负", 400)
        if not lesson_date:
            raise ServiceError("上课日期必填", 400)
        with self.db.transaction() as conn:
            self._require_role(conn, actor_id, ROLE_ADMIN)
            enr = self._row(conn, "SELECT * FROM enrollments WHERE id=?", (enrollment_id,))
            if enr["stage"] not in ("learning",):
                raise Conflict("仅学习阶段记录课时考勤", "stage_invalid")
            aid = _new_id("A")
            conn.execute(
                "INSERT INTO attendance(id,enrollment_id,lesson_date,present,hours,note,created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (aid, enrollment_id, lesson_date, 1 if present else 0,
                 hours if present else 0, note or "", now_iso()),
            )
            if present:
                conn.execute("UPDATE enrollments SET hours=hours+? WHERE id=?",
                             (hours, enrollment_id))
            else:
                conn.execute("UPDATE enrollments SET absences=absences+1 WHERE id=?",
                             (enrollment_id,))
            self._audit(conn, actor_id, "attendance.add", "enrollment", enrollment_id,
                        {"present": bool(present), "hours": hours if present else 0})
            return _rd(conn.execute("SELECT * FROM attendance WHERE id=?", (aid,)).fetchone())

    def add_practice(self, actor_id: str, enrollment_id: str, title: str, passed: bool) -> dict:
        title = (title or "").strip()
        if not title:
            raise ServiceError("实操项目名称必填", 400)
        with self.db.transaction() as conn:
            self._require_role(conn, actor_id, ROLE_ADMIN)
            enr = self._row(conn, "SELECT * FROM enrollments WHERE id=?", (enrollment_id,))
            if enr["stage"] not in ("learning",):
                raise Conflict("仅学习阶段记录实操", "stage_invalid")
            pid = _new_id("P")
            conn.execute(
                "INSERT INTO practicals(id,enrollment_id,title,passed,created_at)"
                " VALUES (?,?,?,?,?)",
                (pid, enrollment_id, title, 1 if passed else 0, now_iso()),
            )
            if passed:
                conn.execute("UPDATE enrollments SET practice_ok=practice_ok+1 WHERE id=?",
                             (enrollment_id,))
            self._audit(conn, actor_id, "practice.add", "enrollment", enrollment_id,
                        {"title": title, "passed": bool(passed)})
            return _rd(conn.execute("SELECT * FROM practicals WHERE id=?", (pid,)).fetchone())

    # ----------------------------------------------------------------- 考核
    def open_assessment(self, actor_id: str, enrollment_id: str,
                        kind: str = "initial", pass_score: int = 60,
                        gap_threshold: int = 15, examiner_required: int = 2) -> dict:
        if kind not in ("initial", "retake"):
            raise ServiceError("普通考核类型只能是 initial/retake（复评由系统发起）", 400)
        try:
            pass_score, gap_threshold = int(pass_score), int(gap_threshold)
            examiner_required = int(examiner_required)
        except (TypeError, ValueError):
            raise ServiceError("分数配置必须是整数", 400)
        if examiner_required < 2:
            raise ServiceError("考核至少需要两名考评员", 400)
        with self.db.transaction() as conn:
            self._require_role(conn, actor_id, ROLE_ADMIN, ROLE_EXAMINER)
            enr = self._row(conn, "SELECT * FROM enrollments WHERE id=?", (enrollment_id,))
            if enr["stage"] not in ("assessment", "certified"):
                raise Conflict("学员尚未进入考核阶段", "stage_invalid")
            course = self._row(conn, "SELECT * FROM courses WHERE id=?", (enr["course_id"],))
            skill_id = course["skill_id"]
            open_chain = conn.execute(
                "SELECT id,status FROM assessments WHERE enrollment_id=? AND skill_id=?"
                " AND result_locked=0 AND parent_id IS NULL",
                (enrollment_id, skill_id),
            ).fetchone()
            if open_chain:
                raise Conflict(
                    "该技能已有进行中考核"
                    + ("（处于复评，请在复评单上评分）" if open_chain["status"] == "in_review"
                       else "（含待评分/待复评）"),
                    "assessment_open",
                )
            xid = _new_id("X")
            conn.execute(
                "INSERT INTO assessments(id,enrollment_id,skill_id,kind,round,parent_id,"
                "status,pass_score,gap_threshold,examiner_required,created_at)"
                " VALUES (?,?,?,?,?,NULL,'open',?,?,?,?)",
                (xid, enrollment_id, skill_id, kind, 1, pass_score,
                 gap_threshold, examiner_required, now_iso()),
            )
            self._audit(conn, actor_id, "assessment.open", "assessment", xid,
                        {"enrollment_id": enrollment_id, "kind": kind})
            return _rd(conn.execute("SELECT * FROM assessments WHERE id=?", (xid,)).fetchone())

    def add_score(self, actor_id: str, assessment_id: str, examiner_id: str,
                  score: int, comment: str = "") -> dict:
        """考评员提交评分。并发安全；重复评分/越权/自评一律拒绝。"""
        try:
            score = int(score)
        except (TypeError, ValueError):
            raise ServiceError("评分必须是 0-100 的整数", 400)
        if not 0 <= score <= 100:
            raise ServiceError("评分必须在 0-100 之间", 400)
        with self.db.transaction() as conn:
            # 操作者必须是考评员，且只能录入本人的打分。
            actor = self._require_role(conn, actor_id, ROLE_EXAMINER)
            if actor["id"] != examiner_id:
                raise Forbidden("考评员只能提交本人的评分，不能代评")
            a = self._row(conn, "SELECT * FROM assessments WHERE id=?", (assessment_id,))
            if a["result_locked"]:
                raise Conflict("考核已结束并锁定，不能重复或补录评分", "assessment_locked")
            if a["status"] == "in_review":
                raise Conflict("该单正在等待复评，请在复评单上评分", "assessment_locked")
            enr = self._row(conn, "SELECT * FROM enrollments WHERE id=?", (a["enrollment_id"],))
            if enr["student_id"] == examiner_id:
                raise Forbidden("考评员不得对本人进行评定")
            dup = conn.execute(
                "SELECT id FROM assessment_scores WHERE assessment_id=? AND examiner_id=?",
                (assessment_id, examiner_id),
            ).fetchone()
            if dup:
                raise Conflict("您已对该考核评分，重复提交被拒绝", "duplicate_score")
            sid = _new_id("SC")
            conn.execute(
                "INSERT INTO assessment_scores(id,assessment_id,examiner_id,score,comment,created_at)"
                " VALUES (?,?,?,?,?,?)",
                (sid, assessment_id, examiner_id, score, comment or "", now_iso()),
            )
            self._audit(conn, actor_id, "score.add", "assessment", assessment_id,
                        {"examiner_id": examiner_id, "score": score})
            count = conn.execute(
                "SELECT COUNT(*) c FROM assessment_scores WHERE assessment_id=?",
                (assessment_id,),
            ).fetchone()["c"]
            # 达到考评员人数后自动评定（分差过大自动转复评）。
            if count >= a["examiner_required"]:
                self._evaluate(conn, a)
            return _rd(conn.execute("SELECT * FROM assessment_scores WHERE id=?", (sid,)).fetchone())

    def _scores(self, conn: sqlite3.Connection, assessment_id: str) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT s.*, u.name examiner_name FROM assessment_scores s"
            " JOIN users u ON u.id=s.examiner_id WHERE s.assessment_id=? ORDER BY s.created_at",
            (assessment_id,),
        ).fetchall()

    def _evaluate(self, conn: sqlite3.Connection, a: sqlite3.Row) -> dict:
        """根据已录入分数判定通过/失败；链头分差过大则自动发起复评。幂势：锁定即返回。"""
        if a["result_locked"]:
            return _rd(a)
        scores = [r["score"] for r in self._scores(conn, a["id"])]
        avg = sum(scores) / len(scores)
        gap = max(scores) - min(scores)
        passed = avg >= a["pass_score"]
        is_chain_head = a["parent_id"] is None
        if gap > a["gap_threshold"] and is_chain_head:
            # 分差过大：链头转 in_review，自动开一张复评单。
            conn.execute(
                "UPDATE assessments SET status='in_review' WHERE id=?", (a["id"],)
            )
            rid = _new_id("X")
            conn.execute(
                "INSERT INTO assessments(id,enrollment_id,skill_id,kind,round,parent_id,"
                "status,pass_score,gap_threshold,examiner_required,created_at)"
                " VALUES (?,?,?,?,?,?,'open',?,?,?,?)",
                (rid, a["enrollment_id"], a["skill_id"], "review", a["round"] + 1, a["id"],
                 a["pass_score"], a["gap_threshold"], max(3, a["examiner_required"]), now_iso()),
            )
            self._audit(conn, None, "assessment.review_open", "assessment", rid,
                        {"parent_id": a["id"], "gap": gap, "scores": scores})
            return _rd(conn.execute("SELECT * FROM assessments WHERE id=?", (rid,)).fetchone())

        terminal = "passed" if passed else "failed"
        conn.execute(
            "UPDATE assessments SET status=?,result_locked=1,finalized_at=? WHERE id=?",
            (terminal, now_iso(), a["id"]),
        )
        self._audit(conn, None, "assessment.finalize", "assessment", a["id"],
                    {"result": terminal, "avg": round(avg, 2), "gap": gap})
        if a["parent_id"] is not None:
            # 复评结论同步回链头：链头同终态并锁定，且链头因有子节点不计入连续合格。
            conn.execute(
                "UPDATE assessments SET status=?,result_locked=1,finalized_at=? WHERE id=?",
                (terminal, now_iso(), a["parent_id"]),
            )
            self._audit(conn, None, "assessment.sync_parent", "assessment", a["parent_id"],
                        {"result": terminal, "from": a["id"]})
        return _rd(conn.execute("SELECT * FROM assessments WHERE id=?", (a["id"],)).fetchone())

    def finalize_assessment(self, actor_id: str, assessment_id: str) -> dict:
        """管理员手动评定：考评员齐了但（例如）未触发自动评定时收口。复评中拒绝。"""
        with self.db.transaction() as conn:
            self._require_role(conn, actor_id, ROLE_ADMIN)
            a = self._row(conn, "SELECT * FROM assessments WHERE id=?", (assessment_id,))
            if a["result_locked"]:
                raise Conflict("考核已结束，重复评定被拒绝", "assessment_locked")
            if a["status"] == "in_review":
                raise Conflict("复评进行中，须在复评单评分收口", "in_review")
            count = conn.execute(
                "SELECT COUNT(*) c FROM assessment_scores WHERE assessment_id=?",
                (assessment_id,),
            ).fetchone()["c"]
            if count < a["examiner_required"]:
                raise Unprocessable(
                    f"考评员不足 {a['examiner_required']} 名，不能评定",
                    "examiners_insufficient", {"actual": count, "need": a["examiner_required"]},
                )
            result = self._evaluate(conn, a)
            return result

    def schedule_review(self, actor_id: str, parent_id: str) -> dict:
        """管理员对分差存疑的考核手动补开复评（自动复评之外的兜底）。"""
        with self.db.transaction() as conn:
            self._require_role(conn, actor_id, ROLE_ADMIN)
            a = self._row(conn, "SELECT * FROM assessments WHERE id=?", (parent_id,))
            if a["result_locked"] and a["status"] in ("passed", "failed"):
                raise Conflict("考核已终态锁定，不能再开复评", "assessment_locked")
            child = conn.execute(
                "SELECT id FROM assessments WHERE parent_id=?", (parent_id,)
            ).fetchone()
            if child:
                raise Conflict("该考核已存在复评单", "review_exists")
            rid = _new_id("X")
            conn.execute(
                "INSERT INTO assessments(id,enrollment_id,skill_id,kind,round,parent_id,"
                "status,pass_score,gap_threshold,examiner_required,created_at)"
                " VALUES (?,?,?,?,?,?,'open',?,?,?,?)",
                (rid, a["enrollment_id"], a["skill_id"], "review", a["round"] + 1, parent_id,
                 a["pass_score"], a["gap_threshold"], max(3, a["examiner_required"]), now_iso()),
            )
            conn.execute("UPDATE assessments SET status='in_review' WHERE id=?", (parent_id,))
            self._audit(conn, actor_id, "assessment.review_open", "assessment", rid,
                        {"parent_id": parent_id, "manual": True})
            return _rd(conn.execute("SELECT * FROM assessments WHERE id=?", (rid,)).fetchone())

    # ------------------------------------------------- 连续合格（终态叶子）
    def consecutive_passes(self, conn: sqlite3.Connection, enrollment_id: str,
                           skill_id: str) -> int:
        """只统计“叶子”终态考核；复评结论以叶子为准，链头不重复计数。

        结论按发生顺序读取：以单调的 rowid 为准（id 本身是随机串、时间戳为秒级
        可能并列，都不能保证先后）。从最新一次起连续通过的次数即为连续合格次数，
        遇到不通过即中断。
        """
        rows = conn.execute(
            "SELECT a.status FROM assessments a WHERE a.enrollment_id=? AND a.skill_id=?"
            " AND a.result_locked=1 AND a.status IN ('passed','failed')"
            " AND NOT EXISTS (SELECT 1 FROM assessments c WHERE c.parent_id=a.id)"
            " ORDER BY a.rowid DESC",
            (enrollment_id, skill_id),
        ).fetchall()
        streak = 0
        for r in rows:
            if r["status"] == "passed":
                streak += 1
            else:
                break
        return streak

    # ----------------------------------------------------------------- 发证
    def issue_certificate(self, actor_id: str, enrollment_id: str,
                          valid_months: int = 12) -> dict:
        try:
            valid_months = int(valid_months)
        except (TypeError, ValueError):
            raise ServiceError("有效期月数必须是整数", 400)
        if valid_months < 1:
            raise ServiceError("证书有效期至少 1 个月", 400)
        with self.db.transaction() as conn:
            self._require_role(conn, actor_id, ROLE_ADMIN)
            enr = self._row(conn, "SELECT * FROM enrollments WHERE id=?", (enrollment_id,))
            course = self._row(conn, "SELECT * FROM courses WHERE id=?", (enr["course_id"],))
            skill_id = course["skill_id"]
            # 必须存在一张通过的、终态的叶子考核（取最近一次，按单调 rowid）。
            leaf = conn.execute(
                "SELECT a.* FROM assessments a WHERE a.enrollment_id=? AND a.skill_id=?"
                " AND a.result_locked=1 AND a.status='passed'"
                " AND NOT EXISTS (SELECT 1 FROM assessments c WHERE c.parent_id=a.id)"
                " ORDER BY a.rowid DESC LIMIT 1",
                (enrollment_id, skill_id),
            ).fetchone()
            if leaf is None:
                raise Unprocessable("尚无通过的考核，不能发证", "no_passing_assessment")
            used = conn.execute(
                "SELECT id FROM qualifications WHERE assessment_id=?", (leaf["id"],)
            ).fetchone()
            if used:
                raise Conflict("该次通过考核已发过证书，不能重复发证", "duplicate_certificate")
            streak = self.consecutive_passes(conn, enrollment_id, skill_id)
            if streak < 1:
                raise Unprocessable("连续合格次数不足，不能发证", "streak_insufficient")

            # 同一学员同一技能只能保留一份有效资格：旧 active 作 superseded(expired)。
            existing = conn.execute(
                "SELECT * FROM qualifications WHERE student_id=? AND skill_id=?"
                " AND status IN ('active','suspended')",
                (enr["student_id"], skill_id),
            ).fetchone()
            issued_at = now_iso()
            valid_until = add_months_iso(issued_at, valid_months)
            qid = _new_id("Q")
            cert_no = f"QCX-{issued_at[:4]}-{uuid.uuid4().hex[:8].upper()}"
            if existing:
                if existing["status"] == "suspended":
                    raise Conflict("现有资格处于暂停状态，请先恢复或撤销，再重新发证",
                                   "qual_suspended")
                conn.execute(
                    "UPDATE qualifications SET status='expired' WHERE id=?", (existing["id"],)
                )
                self._audit(conn, actor_id, "qual.supersede", "qualification", existing["id"],
                            {"by": qid})
            conn.execute(
                "INSERT INTO qualifications(id,student_id,skill_id,assessment_id,cert_no,"
                "status,issued_at,valid_until,consecutive_passes,supersedes_id)"
                " VALUES (?,?,?,?,?,'active',?,?,?,?)",
                (qid, enr["student_id"], skill_id, leaf["id"], cert_no,
                 issued_at, valid_until, streak,
                 existing["id"] if existing else None),
            )
            conn.execute("UPDATE enrollments SET stage='certified' WHERE id=?", (enrollment_id,))
            self._audit(conn, actor_id, "qual.issue", "qualification", qid,
                        {"cert_no": cert_no, "streak": streak, "valid_until": valid_until})
            return _rd(conn.execute("SELECT * FROM qualifications WHERE id=?", (qid,)).fetchone())

    def suspend_qualification(self, actor_id: str, qualification_id: str,
                              reason: str = "") -> dict:
        with self.db.transaction() as conn:
            self._require_role(conn, actor_id, ROLE_ADMIN)
            q = self._row(conn, "SELECT * FROM qualifications WHERE id=?", (qualification_id,))
            if q["status"] != "active":
                raise Conflict(f"资格状态为 {q['status']}，不能暂停", "qual_status")
            conn.execute("UPDATE qualifications SET status='suspended' WHERE id=?",
                         (qualification_id,))
            self._stop_assignments(conn, qualification_id, "资格暂停")
            self._audit(conn, actor_id, "qual.suspend", "qualification", qualification_id,
                        {"reason": reason})
            return _rd(conn.execute("SELECT * FROM qualifications WHERE id=?",
                                    (qualification_id,)).fetchone())

    def revoke_qualification(self, actor_id: str, qualification_id: str,
                             reason: str = "") -> dict:
        with self.db.transaction() as conn:
            self._require_role(conn, actor_id, ROLE_ADMIN)
            q = self._row(conn, "SELECT * FROM qualifications WHERE id=?", (qualification_id,))
            if q["status"] not in ("active", "suspended"):
                raise Conflict(f"资格状态为 {q['status']}，不能撤销", "qual_status")
            conn.execute("UPDATE qualifications SET status='revoked' WHERE id=?",
                         (qualification_id,))
            self._stop_assignments(conn, qualification_id, f"资格撤销：{reason or '无'}")
            self._audit(conn, actor_id, "qual.revoke", "qualification", qualification_id,
                        {"reason": reason})
            return _rd(conn.execute("SELECT * FROM qualifications WHERE id=?",
                                    (qualification_id,)).fetchone())

    def restore_qualification(self, actor_id: str, qualification_id: str) -> dict:
        with self.db.transaction() as conn:
            self._require_role(conn, actor_id, ROLE_ADMIN)
            q = self._row(conn, "SELECT * FROM qualifications WHERE id=?", (qualification_id,))
            if q["status"] != "suspended":
                raise Conflict("仅暂停中的资格可以恢复", "qual_status")
            if q["valid_until"] <= now_iso():
                conn.execute("UPDATE qualifications SET status='expired' WHERE id=?",
                             (qualification_id,))
                raise Conflict("资格已过期，暂停恢复被拒绝，须重新考核发证", "qual_expired")
            # 恢复后仍受“仅一份有效资格”约束（正常不会冲突，索引兜底）。
            conn.execute("UPDATE qualifications SET status='active' WHERE id=?",
                         (qualification_id,))
            self._audit(conn, actor_id, "qual.restore", "qualification", qualification_id, {})
            return _rd(conn.execute("SELECT * FROM qualifications WHERE id=?",
                                    (qualification_id,)).fetchone())

    def _stop_assignments(self, conn: sqlite3.Connection, qualification_id: str,
                          reason: str) -> int:
        cur = conn.execute(
            "UPDATE assignments SET status='ended',end_reason=?,ended_at=? "
            "WHERE qualification_id=? AND status='active'",
            (reason, now_iso(), qualification_id),
        )
        return cur.rowcount

    def expire_qualifications(self) -> int:
        """资格到期：置 expired 并立即停止其关键工序派工。启动与派工前调用。"""
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT id FROM qualifications WHERE status IN ('active','suspended')"
                " AND valid_until<=?",
                (now_iso(),),
            ).fetchall()
            for r in rows:
                conn.execute("UPDATE qualifications SET status='expired' WHERE id=?", (r["id"],))
                stopped = self._stop_assignments(conn, r["id"], "资格过期")
                self._audit(conn, None, "qual.expire", "qualification", r["id"],
                            {"assignments_stopped": stopped})
            return len(rows)

    def dev_mark_expired(self, actor_id: str, qualification_id: str) -> dict:
        """演练辅助：把有效期改到过去并立即按到期处理（生产可禁用该路由）。"""
        from .util import add_months_iso  # 局部引用避免循环观感
        with self.db.transaction() as conn:
            self._require_role(conn, actor_id, ROLE_ADMIN)
            self._row(conn, "SELECT id FROM qualifications WHERE id=?", (qualification_id,))
            past = add_months_iso(now_iso(), -1)
            conn.execute("UPDATE qualifications SET valid_until=? WHERE id=?",
                         (past, qualification_id))
            n = self.expire_in_tx(conn)
            self._audit(conn, actor_id, "qual.dev_expire", "qualification",
                        qualification_id, {"expired_now": n})
            return _rd(conn.execute("SELECT * FROM qualifications WHERE id=?",
                                    (qualification_id,)).fetchone())

    # ----------------------------------------------------------------- 派工
    def assign_workstation(self, actor_id: str, student_id: str, workstation_id: str) -> dict:
        with self.db.transaction() as conn:
            self._require_role(conn, actor_id, ROLE_ADMIN)
            self.expire_in_tx(conn)
            student = self._user(conn, student_id)
            if student["role"] != ROLE_STUDENT:
                raise ServiceError("只能向学员派工", 400)
            ws = self._row(conn, "SELECT * FROM workstations WHERE id=?", (workstation_id,))
            if not ws["active"]:
                raise Conflict("工位已停用", "workstation_inactive")
            # 同一学员只能有一份在岗派工。
            busy_student = conn.execute(
                "SELECT id FROM assignments WHERE student_id=? AND status='active'",
                (student_id,),
            ).fetchone()
            if busy_student:
                raise Conflict("该学员已有在岗派工，须先结束现有派工", "student_busy")
            # 同一工位同时只能有一人在岗。
            busy_station = conn.execute(
                "SELECT id FROM assignments WHERE workstation_id=? AND status='active'",
                (workstation_id,),
            ).fetchone()
            if busy_station:
                raise Conflict("该工位已有在岗人员", "workstation_busy")

            qual_row = None
            if ws["is_key"]:
                if not ws["skill_id"]:
                    raise ServiceError("关键工序工位缺少绑定技能", 500)
                qual_row = conn.execute(
                    "SELECT * FROM qualifications WHERE student_id=? AND skill_id=?"
                    " AND status='active'",
                    (student_id, ws["skill_id"]),
                ).fetchone()
                if qual_row is None:
                    raise Unprocessable(
                        "关键工序要求有效资格：学员缺少该技能的有效（未过期/未暂停/未撤销）资格",
                        "no_valid_qualification",
                    )
                if qual_row["valid_until"] <= now_iso():
                    raise Unprocessable("资格已过期，不得从事关键工序", "qual_expired")
                skill = self._row(conn, "SELECT * FROM skills WHERE id=?", (ws["skill_id"],))
                enr = conn.execute(
                    "SELECT id FROM enrollments WHERE student_id=? AND course_id IN"
                    " (SELECT id FROM courses WHERE skill_id=?) ORDER BY rowid DESC LIMIT 1",
                    (student_id, ws["skill_id"]),
                ).fetchone()
                streak = (self.consecutive_passes(conn, enr["id"], ws["skill_id"])
                          if enr else 0)
                if streak < skill["min_consecutive"]:
                    raise Unprocessable(
                        f"关键工序要求连续合格 {skill['min_consecutive']} 次，当前 {streak} 次",
                        "streak_insufficient",
                        {"actual": streak, "need": skill["min_consecutive"]},
                    )
            else:
                # 非关键工序：至少是在册学员。
                enr = conn.execute(
                    "SELECT id FROM enrollments WHERE student_id=? LIMIT 1", (student_id,)
                ).fetchone()
                if enr is None:
                    raise Unprocessable("该学员尚未报名任何课程，暂不能派工", "not_enrolled")

            jid = _new_id("J")
            conn.execute(
                "INSERT INTO assignments(id,student_id,workstation_id,qualification_id,"
                "status,created_at) VALUES (?,?,?,?,'active',?)",
                (jid, student_id, workstation_id,
                 qual_row["id"] if qual_row else None, now_iso()),
            )
            self._audit(conn, actor_id, "assignment.create", "assignment", jid,
                        {"student_id": student_id, "workstation_id": workstation_id,
                         "key": bool(ws["is_key"])})
            return self.get_assignment(jid, conn=conn)

    def expire_in_tx(self, conn: sqlite3.Connection) -> int:
        rows = conn.execute(
            "SELECT id FROM qualifications WHERE status IN ('active','suspended')"
            " AND valid_until<=?",
            (now_iso(),),
        ).fetchall()
        for r in rows:
            conn.execute("UPDATE qualifications SET status='expired' WHERE id=?", (r["id"],))
            self._stop_assignments(conn, r["id"], "资格过期")
            self._audit(conn, None, "qual.expire", "qualification", r["id"], {"on": "assign"})
        return len(rows)

    def end_assignment(self, actor_id: str, assignment_id: str, reason: str = "") -> dict:
        with self.db.transaction() as conn:
            self._require_role(conn, actor_id, ROLE_ADMIN)
            a = self._row(conn, "SELECT * FROM assignments WHERE id=?", (assignment_id,))
            if a["status"] != "active":
                raise Conflict("派工已结束", "assignment_ended")
            conn.execute(
                "UPDATE assignments SET status='ended',end_reason=?,ended_at=? WHERE id=?",
                (reason or "人工结束", now_iso(), assignment_id),
            )
            self._audit(conn, actor_id, "assignment.end", "assignment", assignment_id,
                        {"reason": reason})
            return self.get_assignment(assignment_id, conn=conn)

    # ----------------------------------------------------------------- 查询
    def list(self, table: str) -> list[dict]:
        allowed = {
            "users": "SELECT * FROM users ORDER BY created_at",
            "skills": "SELECT * FROM skills ORDER BY created_at",
            "courses": "SELECT * FROM courses ORDER BY created_at",
            "workstations": "SELECT * FROM workstations ORDER BY created_at",
            "enrollments": "SELECT * FROM enrollments ORDER BY created_at",
            "qualifications": "SELECT * FROM qualifications ORDER BY issued_at DESC",
            "assignments": "SELECT * FROM assignments ORDER BY created_at DESC",
            "assessments": "SELECT * FROM assessments ORDER BY created_at DESC",
            "audit_log": "SELECT * FROM audit_log ORDER BY id DESC LIMIT 200",
        }
        if table not in allowed:
            raise NotFound("未知资源")
        conn = self.db.connect()
        try:
            rows = conn.execute(allowed[table]).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_enrollment(self, enrollment_id: str, conn: sqlite3.Connection | None = None) -> dict:
        own = conn is None
        if own:
            conn = self.db.connect()
        try:
            enr = self._row(conn, "SELECT * FROM enrollments WHERE id=?", (enrollment_id,))
            data = dict(enr)
            course = conn.execute("SELECT * FROM courses WHERE id=?",
                                  (enr["course_id"],)).fetchone()
            student = conn.execute("SELECT * FROM users WHERE id=?",
                                   (enr["student_id"],)).fetchone()
            skill = course and conn.execute("SELECT * FROM skills WHERE id=?",
                                            (course["skill_id"],)).fetchone()
            data["course"] = dict(course) if course else None
            data["student"] = dict(student) if student else None
            data["skill"] = dict(skill) if skill else None
            data["gate"] = self.gate_status(enrollment_id, conn=conn)
            data["attendance"] = [dict(r) for r in conn.execute(
                "SELECT * FROM attendance WHERE enrollment_id=? ORDER BY lesson_date",
                (enrollment_id,)).fetchall()]
            data["practicals"] = [dict(r) for r in conn.execute(
                "SELECT * FROM practicals WHERE enrollment_id=? ORDER BY created_at",
                (enrollment_id,)).fetchall()]
            data["assessments"] = [dict(r) for r in conn.execute(
                "SELECT * FROM assessments WHERE enrollment_id=? ORDER BY created_at",
                (enrollment_id,)).fetchall()]
            data["consecutive_passes"] = self.consecutive_passes(
                conn, enrollment_id, course["skill_id"]) if course else 0
            return data
        finally:
            if own:
                conn.close()

    def get_assessment(self, assessment_id: str) -> dict:
        conn = self.db.connect()
        try:
            a = self._row(conn, "SELECT * FROM assessments WHERE id=?", (assessment_id,))
            data = dict(a)
            data["scores"] = [dict(r) for r in self._scores(conn, assessment_id)]
            data["children"] = [dict(r) for r in conn.execute(
                "SELECT * FROM assessments WHERE parent_id=?", (assessment_id,)).fetchall()]
            enr = conn.execute("SELECT * FROM enrollments WHERE id=?",
                               (a["enrollment_id"],)).fetchone()
            data["enrollment"] = dict(enr) if enr else None
            return data
        finally:
            conn.close()

    def get_assignment(self, assignment_id: str,
                       conn: sqlite3.Connection | None = None) -> dict:
        own = conn is None
        if own:
            conn = self.db.connect()
        try:
            a = self._row(conn, "SELECT * FROM assignments WHERE id=?", (assignment_id,))
            data = dict(a)
            data["student"] = dict(conn.execute(
                "SELECT * FROM users WHERE id=?", (a["student_id"],)).fetchone())
            data["workstation"] = dict(conn.execute(
                "SELECT * FROM workstations WHERE id=?", (a["workstation_id"],)).fetchone())
            if a["qualification_id"]:
                data["qualification"] = dict(conn.execute(
                    "SELECT * FROM qualifications WHERE id=?",
                    (a["qualification_id"],)).fetchone())
            return data
        finally:
            if own:
                conn.close()

    def get_qualification(self, qualification_id: str) -> dict:
        conn = self.db.connect()
        try:
            q = self._row(conn, "SELECT * FROM qualifications WHERE id=?", (qualification_id,))
            data = dict(q)
            data["student"] = dict(conn.execute(
                "SELECT * FROM users WHERE id=?", (q["student_id"],)).fetchone())
            data["skill"] = dict(conn.execute(
                "SELECT * FROM skills WHERE id=?", (q["skill_id"],)).fetchone())
            return data
        finally:
            conn.close()
