"""服务层：门控、考核（复评/越权/重复）、发证唯一资格、资格生命周期、派工与回滚。"""
from __future__ import annotations

import sqlite3

from app.services import (
    Conflict,
    Forbidden,
    ServiceError,
    Unprocessable,
)
from tests.helpers import ServiceTestCase


class TestGateAndEnrollment(ServiceTestCase):
    def test_duplicate_enrollment_rejected(self):
        w = self.w
        e = w.s.create_enrollment(w.admin["id"], w.stu[0]["id"], w.course_plain["id"])
        with self.assertRaises(Conflict) as cm:
            w.s.create_enrollment(w.admin["id"], w.stu[0]["id"], w.course_plain["id"])
        self.assertEqual(cm.exception.code, "duplicate_enrollment")

    def test_advance_requires_stage_order(self):
        w = self.w
        e = w.s.create_enrollment(w.admin["id"], w.stu[0]["id"], w.course_plain["id"])
        # 未进入学习不能直接进考核
        with self.assertRaises(Conflict):
            w.s.advance_to_assessment(w.admin["id"], e["id"])

    def test_gate_blocks_on_hours_practice_absences(self):
        w = self.w
        e = w.s.create_enrollment(w.admin["id"], w.stu[0]["id"], w.course_plain["id"])
        w.s.start_learning(w.admin["id"], e["id"])
        # 课时不足 + 实操不足
        with self.assertRaises(Unprocessable) as cm:
            w.s.advance_to_assessment(w.admin["id"], e["id"])
        gate = cm.exception.details
        self.assertFalse(gate["ready"])
        self.assertFalse(gate["checks"]["hours"]["ok"])
        self.assertFalse(gate["checks"]["practice"]["ok"])
        # 补足课时与实操
        w.s.add_attendance(w.admin["id"], e["id"], "2026-08-01", True, 8)
        w.s.add_practice(w.admin["id"], e["id"], "p1", True)
        w.s.add_practice(w.admin["id"], e["id"], "p2", True)
        out = w.s.advance_to_assessment(w.admin["id"], e["id"])
        self.assertEqual(out["stage"], "assessment")

    def test_absences_over_limit_blocks(self):
        w = self.w
        e = w.s.create_enrollment(w.admin["id"], w.stu[0]["id"], w.course_plain["id"])
        w.s.start_learning(w.admin["id"], e["id"])
        w.s.add_attendance(w.admin["id"], e["id"], "2026-08-01", True, 8)
        w.s.add_practice(w.admin["id"], e["id"], "p1", True)
        w.s.add_practice(w.admin["id"], e["id"], "p2", True)
        # allowed_absences=1，记 2 次缺课
        w.s.add_attendance(w.admin["id"], e["id"], "2026-08-02", False, 0)
        w.s.add_attendance(w.admin["id"], e["id"], "2026-08-03", False, 0)
        with self.assertRaises(Unprocessable) as cm:
            w.s.advance_to_assessment(w.admin["id"], e["id"])
        self.assertFalse(cm.exception.details["checks"]["absences"]["ok"])

    def test_cannot_record_after_stage_advanced(self):
        w = self.w
        e = w.ready_enrollment(w.stu[0], w.course_plain)
        with self.assertRaises(Conflict):
            w.s.add_attendance(w.admin["id"], e["id"], "2026-08-09", True, 8)
        with self.assertRaises(Conflict):
            w.s.add_practice(w.admin["id"], e["id"], "late", True)

    def test_only_admin_sets_up_master_data(self):
        w = self.w
        # 学员/考评员不能建课程、技能、工位、报名
        for actor in (w.stu[0]["id"], w.ex[0]["id"], None):
            with self.assertRaises(Forbidden):
                w.s.create_course(actor, "x", w.skill_plain["id"], 1, 1, 0)
            with self.assertRaises(Forbidden):
                w.s.create_skill(actor, "x")
            with self.assertRaises(Forbidden):
                w.s.create_enrollment(actor, w.stu[1]["id"], w.course_plain["id"])

    def test_user_creation_requires_admin_over_http_semantics(self):
        # 缺少身份（None 且非 system）必须拒绝，防止越权建号。
        with self.assertRaises(Forbidden):
            self.w.s.create_user(None, "黑客", "admin")
        # 学员不能建管理员
        with self.assertRaises(Forbidden):
            self.w.s.create_user(self.w.stu[0]["id"], "黑客2", "admin")


class TestAssessmentScoring(ServiceTestCase):
    def _open(self, eid, **kw):
        return self.w.s.open_assessment(self.w.admin["id"], eid, **kw)

    def test_requires_two_examiners_to_finalize(self):
        w = self.w
        e = w.ready_enrollment(w.stu[0], w.course_plain)
        a = self._open(e["id"])
        w.s.add_score(w.ex[0]["id"], a["id"], w.ex[0]["id"], 90)
        with self.assertRaises(Unprocessable) as cm:
            w.s.finalize_assessment(w.admin["id"], a["id"])
        self.assertEqual(cm.exception.details, {"actual": 1, "need": 2})
        # 第二名补齐后自动评定
        w.s.add_score(w.ex[1]["id"], a["id"], w.ex[1]["id"], 88)
        self.assertEqual(w.s.get_assessment(a["id"])["status"], "passed")

    def test_pass_and_fail_threshold(self):
        w = self.w
        e = w.ready_enrollment(w.stu[0], w.course_plain)
        a = self._open(e["id"], pass_score=70)
        w.s.add_score(w.ex[0]["id"], a["id"], w.ex[0]["id"], 60)
        w.s.add_score(w.ex[1]["id"], a["id"], w.ex[1]["id"], 62)
        self.assertEqual(w.s.get_assessment(a["id"])["status"], "failed")

    def test_duplicate_score_same_examiner_rejected(self):
        w = self.w
        e = w.ready_enrollment(w.stu[0], w.course_plain)
        a = self._open(e["id"])
        w.s.add_score(w.ex[0]["id"], a["id"], w.ex[0]["id"], 80)
        with self.assertRaises(Conflict) as cm:
            w.s.add_score(w.ex[0]["id"], a["id"], w.ex[0]["id"], 99)
        self.assertEqual(cm.exception.code, "duplicate_score")

    def test_score_out_of_range_rejected(self):
        w = self.w
        e = w.ready_enrollment(w.stu[0], w.course_plain)
        a = self._open(e["id"])
        for bad in (-1, 101):
            with self.assertRaises(ServiceError):
                w.s.add_score(w.ex[0]["id"], a["id"], w.ex[0]["id"], bad)

    def test_non_examiner_and_proxy_scoring_rejected(self):
        w = self.w
        e = w.ready_enrollment(w.stu[0], w.course_plain)
        a = self._open(e["id"])
        # 学员评分：越权
        with self.assertRaises(Forbidden):
            w.s.add_score(w.stu[0]["id"], a["id"], w.stu[0]["id"], 90)
        # 管理员不是考评员，也不能评分
        with self.assertRaises(Forbidden):
            w.s.add_score(w.admin["id"], a["id"], w.admin["id"], 90)
        # 考评员 A 冒充提交 B 的分数（代评）：越权
        with self.assertRaises(Forbidden):
            w.s.add_score(w.ex[0]["id"], a["id"], w.ex[1]["id"], 90)

    def test_only_two_distinct_examiners_count(self):
        # UNIQUE(assessment,examiner) 保证不会出现“同一人两次凑够两名”
        w = self.w
        e = w.ready_enrollment(w.stu[0], w.course_plain)
        a = self._open(e["id"])
        w.s.add_score(w.ex[0]["id"], a["id"], w.ex[0]["id"], 70)
        with self.assertRaises(Conflict):
            w.s.add_score(w.ex[0]["id"], a["id"], w.ex[0]["id"], 75)
        # 仍未达标
        with self.assertRaises(Unprocessable):
            w.s.finalize_assessment(w.admin["id"], a["id"])

    def test_locked_assessment_rejects_more_scores_and_finalize(self):
        w = self.w
        e = w.ready_enrollment(w.stu[0], w.course_plain)
        a = self._open(e["id"])
        w.s.add_score(w.ex[0]["id"], a["id"], w.ex[0]["id"], 80)
        w.s.add_score(w.ex[1]["id"], a["id"], w.ex[1]["id"], 82)
        with self.assertRaises(Conflict) as cm:
            w.s.add_score(w.ex[2]["id"], a["id"], w.ex[2]["id"], 85)
        self.assertEqual(cm.exception.code, "assessment_locked")
        with self.assertRaises(Conflict):
            w.s.finalize_assessment(w.admin["id"], a["id"])


class TestReviewFlow(ServiceTestCase):
    def test_large_gap_auto_opens_review_requiring_three(self):
        w = self.w
        e = w.ready_enrollment(w.stu[0], w.course_plain)
        a = w.s.open_assessment(w.admin["id"], e["id"], gap_threshold=15)
        w.s.add_score(w.ex[0]["id"], a["id"], w.ex[0]["id"], 95)
        w.s.add_score(w.ex[1]["id"], a["id"], w.ex[1]["id"], 60)  # gap 35
        head = w.s.get_assessment(a["id"])
        self.assertEqual(head["status"], "in_review")
        self.assertEqual(len(head["children"]), 1)
        rid = head["children"][0]["id"]
        review = w.s.get_assessment(rid)
        self.assertEqual(review["kind"], "review")
        self.assertGreaterEqual(review["examiner_required"], 3)
        # 在原单上补分被拒（须在复评单评分）
        with self.assertRaises(Conflict):
            w.s.add_score(w.ex[2]["id"], a["id"], w.ex[2]["id"], 80)

    def test_review_conclusion_syncs_head_and_counts_once(self):
        w = self.w
        e = w.ready_enrollment(w.stu[0], w.course_plain)
        a = w.s.open_assessment(w.admin["id"], e["id"], gap_threshold=15)
        w.s.add_score(w.ex[0]["id"], a["id"], w.ex[0]["id"], 95)
        w.s.add_score(w.ex[1]["id"], a["id"], w.ex[1]["id"], 50)
        rid = w.s.get_assessment(a["id"])["children"][0]["id"]
        w.s.add_score(w.ex[0]["id"], rid, w.ex[0]["id"], 80)
        w.s.add_score(w.ex[1]["id"], rid, w.ex[1]["id"], 82)
        # 复评需 3 名，2 名不能收口
        with self.assertRaises(Unprocessable):
            w.s.finalize_assessment(w.admin["id"], rid)
        w.s.add_score(w.ex[2]["id"], rid, w.ex[2]["id"], 84)
        leaf = w.s.get_assessment(rid)
        head = w.s.get_assessment(a["id"])
        self.assertEqual(leaf["status"], "passed")
        self.assertEqual(head["status"], "passed")
        self.assertTrue(head["result_locked"])
        # 叶子计数一次，链头不重复：连续合格 == 1
        self.assertEqual(w.s.get_enrollment(e["id"])["consecutive_passes"], 1)

    def test_small_gap_does_not_trigger_review(self):
        w = self.w
        e = w.ready_enrollment(w.stu[0], w.course_plain)
        a = w.s.open_assessment(w.admin["id"], e["id"], gap_threshold=15)
        w.s.add_score(w.ex[0]["id"], a["id"], w.ex[0]["id"], 80)
        w.s.add_score(w.ex[1]["id"], a["id"], w.ex[1]["id"], 90)  # gap 10
        self.assertEqual(w.s.get_assessment(a["id"])["status"], "passed")
        self.assertEqual(w.s.get_assessment(a["id"])["children"], [])

    def test_cannot_open_second_open_assessment(self):
        w = self.w
        e = w.ready_enrollment(w.stu[0], w.course_plain)
        w.s.open_assessment(w.admin["id"], e["id"])
        with self.assertRaises(Conflict) as cm:
            w.s.open_assessment(w.admin["id"], e["id"])
        self.assertEqual(cm.exception.code, "assessment_open")

    def test_manual_review_only_when_no_child(self):
        w = self.w
        e = w.ready_enrollment(w.stu[0], w.course_plain)
        a = w.s.open_assessment(w.admin["id"], e["id"])
        w.s.schedule_review(w.admin["id"], a["id"])
        with self.assertRaises(Conflict):  # 已有复评单
            w.s.schedule_review(w.admin["id"], a["id"])

    def test_failed_review_marks_head_failed(self):
        w = self.w
        e = w.ready_enrollment(w.stu[0], w.course_plain)
        a = w.s.open_assessment(w.admin["id"], e["id"], gap_threshold=15, pass_score=70)
        w.s.add_score(w.ex[0]["id"], a["id"], w.ex[0]["id"], 100)
        w.s.add_score(w.ex[1]["id"], a["id"], w.ex[1]["id"], 20)
        rid = w.s.get_assessment(a["id"])["children"][0]["id"]
        for i in range(3):
            w.s.add_score(w.ex[i]["id"], rid, w.ex[i]["id"], 40)
        self.assertEqual(w.s.get_assessment(rid)["status"], "failed")
        self.assertEqual(w.s.get_assessment(a["id"])["status"], "failed")


class TestCertification(ServiceTestCase):
    def test_cert_requires_passing_assessment(self):
        w = self.w
        e = w.ready_enrollment(w.stu[0], w.course_plain)
        # 没考核
        with self.assertRaises(Unprocessable) as cm:
            w.certify(e["id"])
        self.assertEqual(cm.exception.code, "no_passing_assessment")
        # 考了但没过
        w.assess(e["id"], (50, 55), pass_score=70)
        with self.assertRaises(Unprocessable):
            w.certify(e["id"])

    def test_cannot_issue_twice_from_same_assessment(self):
        w = self.w
        e = w.ready_enrollment(w.stu[0], w.course_plain)
        w.assess(e["id"], (80, 82))
        w.certify(e["id"])
        with self.assertRaises(Conflict) as cm:
            w.certify(e["id"])
        self.assertEqual(cm.exception.code, "duplicate_certificate")

    def test_only_one_valid_qualification_supersedes_old(self):
        w = self.w
        e = w.ready_enrollment(w.stu[0], w.course_plain)
        w.assess(e["id"], (80, 82))
        q1 = w.certify(e["id"])
        # 再来一次通过考核并发新证（旧证作过期处理）
        w.assess(e["id"], (85, 87), kind="retake")
        q2 = w.s.issue_certificate(w.admin["id"], e["id"], 12)
        self.assertEqual(q2["supersedes_id"], q1["id"])
        conn = self.db.connect()
        try:
            valid = conn.execute(
                "SELECT COUNT(*) c FROM qualifications WHERE student_id=? AND skill_id=?"
                " AND status IN ('active','suspended')",
                (w.stu[0]["id"], w.skill_plain["id"])).fetchone()["c"]
            self.assertEqual(valid, 1)
            old = conn.execute("SELECT status FROM qualifications WHERE id=?",
                               (q1["id"],)).fetchone()["status"]
            self.assertEqual(old, "expired")
        finally:
            conn.close()

    def test_reissue_blocked_while_suspended(self):
        w = self.w
        e = w.ready_enrollment(w.stu[0], w.course_plain)
        w.assess(e["id"], (80, 82))
        q = w.certify(e["id"])
        w.s.suspend_qualification(w.admin["id"], q["id"], "调查中")
        w.assess(e["id"], (90, 92), kind="retake")
        with self.assertRaises(Conflict) as cm:
            w.s.issue_certificate(w.admin["id"], e["id"], 12)
        self.assertEqual(cm.exception.code, "qual_suspended")

    def test_only_admin_issues_or_changes_qualification(self):
        w = self.w
        e = w.ready_enrollment(w.stu[0], w.course_plain)
        w.assess(e["id"], (80, 82))
        with self.assertRaises(Forbidden):
            w.s.issue_certificate(w.ex[0]["id"], e["id"], 12)


class TestQualificationLifecycleAndAssignment(ServiceTestCase):
    def _key_qualified_student(self, idx=0):
        e, q, _ = self.w.qualify_streak(
            self.w.stu[idx], self.w.course_key, scores_per_round=((80, 82), (85, 87)))
        return e, q

    def test_key_assignment_requires_consecutive_passes(self):
        w = self.w
        # 只合格 1 次：关键工序被拦
        e = w.ready_enrollment(w.stu[0], w.course_key)
        w.assess(e["id"], (80, 82))
        w.certify(e["id"])
        with self.assertRaises(Unprocessable) as cm:
            w.s.assign_workstation(w.admin["id"], w.stu[0]["id"], w.ws_key["id"])
        self.assertEqual(cm.exception.code, "streak_insufficient")
        self.assertEqual(cm.exception.details, {"actual": 1, "need": 2})

    def test_key_assignment_after_two_streak_and_valid_cert(self):
        w = self.w
        _, q = self._key_qualified_student(0)
        j = w.s.assign_workstation(w.admin["id"], w.stu[0]["id"], w.ws_key["id"])
        self.assertEqual(j["status"], "active")
        self.assertEqual(j["qualification_id"], q["id"])

    def test_station_and_student_mutual_exclusion(self):
        w = self.w
        self._key_qualified_student(0)
        self._key_qualified_student(1)
        w.s.assign_workstation(w.admin["id"], w.stu[0]["id"], w.ws_key["id"])
        # 工位占用
        with self.assertRaises(Conflict) as cm:
            w.s.assign_workstation(w.admin["id"], w.stu[1]["id"], w.ws_key["id"])
        self.assertEqual(cm.exception.code, "workstation_busy")
        # 学员占用
        with self.assertRaises(Conflict) as cm:
            w.s.assign_workstation(w.admin["id"], w.stu[0]["id"], w.ws_key2["id"])
        self.assertEqual(cm.exception.code, "student_busy")

    def test_suspend_and_revoke_stop_assignment_immediately(self):
        w = self.w
        _, q = self._key_qualified_student(0)
        j = w.s.assign_workstation(w.admin["id"], w.stu[0]["id"], w.ws_key["id"])
        w.s.suspend_qualification(w.admin["id"], q["id"], "违规操作")
        self.assertEqual(w.s.get_assignment(j["id"])["status"], "ended")
        self.assertIn("暂停", w.s.get_assignment(j["id"])["end_reason"])
        # 暂停期间不能再派关键工序
        with self.assertRaises(Unprocessable):
            w.s.assign_workstation(w.admin["id"], w.stu[0]["id"], w.ws_key2["id"])

        # 恢复后可再派
        w.s.restore_qualification(w.admin["id"], q["id"])
        j2 = w.s.assign_workstation(w.admin["id"], w.stu[0]["id"], w.ws_key2["id"])
        self.assertEqual(j2["status"], "active")
        # 撤销立即停派
        w.s.revoke_qualification(w.admin["id"], q["id"], "严重违规")
        self.assertEqual(w.s.get_assignment(j2["id"])["status"], "ended")
        with self.assertRaises(Unprocessable):
            w.s.assign_workstation(w.admin["id"], w.stu[0]["id"], w.ws_key["id"])

    def test_expired_cert_blocks_and_sweep_stops_assignment(self):
        w = self.w
        _, q = self._key_qualified_student(0)
        j = w.s.assign_workstation(w.admin["id"], w.stu[0]["id"], w.ws_key["id"])
        # 用演练接口把有效期改到过去（同一事务内已置 expired 并停派工）。
        w.s.dev_mark_expired(w.admin["id"], q["id"])
        self.assertEqual(w.s.get_qualification(q["id"])["status"], "expired")
        # 旧派工已立即结束
        self.assertEqual(w.s.get_assignment(j["id"])["status"], "ended")
        # 再清扫是幂等的：不会重复处理
        self.assertEqual(w.s.expire_qualifications(), 0)
        # 过期后不能再派关键工序
        with self.assertRaises(Unprocessable) as cm:
            w.s.assign_workstation(w.admin["id"], w.stu[0]["id"], w.ws_key2["id"])
        self.assertEqual(cm.exception.code, "no_valid_qualification")

    def test_restore_expired_suspended_is_refused(self):
        w = self.w
        _, q = self._key_qualified_student(0)
        w.s.suspend_qualification(w.admin["id"], q["id"])
        w.s.dev_mark_expired(w.admin["id"], q["id"])
        w.s.expire_qualifications()
        with self.assertRaises(Conflict):
            w.s.restore_qualification(w.admin["id"], q["id"])

    def test_failure_resets_consecutive_streak(self):
        w = self.w
        e = w.ready_enrollment(w.stu[0], w.course_key)
        w.assess(e["id"], (80, 82))     # streak 1
        w.assess(e["id"], (30, 32), kind="retake", pass_score=60)  # fail -> 0
        self.assertEqual(w.s.get_enrollment(e["id"])["consecutive_passes"], 0)
        w.assess(e["id"], (80, 82), kind="retake")  # streak 1 again
        self.assertEqual(w.s.get_enrollment(e["id"])["consecutive_passes"], 1)

    def test_non_key_station_needs_enrollment_only(self):
        w = self.w
        # 未报名任何课程
        with self.assertRaises(Unprocessable):
            w.s.assign_workstation(w.admin["id"], w.stu[2]["id"], w.ws_plain["id"])
        e = w.s.create_enrollment(w.admin["id"], w.stu[2]["id"], w.course_plain["id"])
        w.s.start_learning(w.admin["id"], e["id"])
        j = w.s.assign_workstation(w.admin["id"], w.stu[2]["id"], w.ws_plain["id"])
        self.assertEqual(j["status"], "active")

    def test_student_cannot_assign(self):
        w = self.w
        with self.assertRaises(Forbidden):
            w.s.assign_workstation(w.stu[0]["id"], w.stu[0]["id"], w.ws_plain["id"])


class TestRollback(ServiceTestCase):
    def test_failed_gate_leaves_no_partial_stage_change(self):
        w = self.w
        e = w.s.create_enrollment(w.admin["id"], w.stu[0]["id"], w.course_plain["id"])
        w.s.start_learning(w.admin["id"], e["id"])
        before = w.s.get_enrollment(e["id"])
        with self.assertRaises(Unprocessable):
            w.s.advance_to_assessment(w.admin["id"], e["id"])
        after = w.s.get_enrollment(e["id"])
        self.assertEqual(after["stage"], before["stage"])
        self.assertEqual(after["stage"], "learning")

    def test_duplicate_score_transaction_rolls_back_audit(self):
        w = self.w
        e = w.ready_enrollment(w.stu[0], w.course_plain)
        a = w.s.open_assessment(w.admin["id"], e["id"])
        w.s.add_score(w.ex[0]["id"], a["id"], w.ex[0]["id"], 80)
        conn = self.db.connect()
        try:
            audits_before = conn.execute(
                "SELECT COUNT(*) c FROM audit_log WHERE action='score.add'").fetchone()["c"]
        finally:
            conn.close()
        with self.assertRaises(Conflict):
            w.s.add_score(w.ex[0]["id"], a["id"], w.ex[0]["id"], 99)
        conn = self.db.connect()
        try:
            audits_after = conn.execute(
                "SELECT COUNT(*) c FROM audit_log WHERE action='score.add'").fetchone()["c"]
            scores = conn.execute(
                "SELECT COUNT(*) c FROM assessment_scores WHERE assessment_id=?",
                (a["id"],)).fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(audits_after, audits_before)  # 审计未脏写
        self.assertEqual(scores, 1)

    def test_integrity_violation_rolls_back_whole_tx(self):
        # 直接在事务中制造唯一约束冲突，确认 BEGIN IMMEDIATE 事务整体回滚。
        db = self.db
        with self.assertRaises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO skills(id,name,key_process,min_consecutive,created_at)"
                    " VALUES ('DUP','x',0,1,'t')")
                conn.execute(
                    "INSERT INTO skills(id,name,key_process,min_consecutive,created_at)"
                    " VALUES ('DUP','y',0,1,'t')")
        conn = db.connect()
        try:
            # 第一条也必须随事务回滚，库里不应出现 'x'
            self.assertIsNone(conn.execute(
                "SELECT id FROM skills WHERE id='DUP'").fetchone())
        finally:
            conn.close()

    def test_review_open_then_failure_keeps_consistency(self):
        # 复评单需要 3 名；只给 2 名时链头保持 in_review、叶子 open，不产生终态。
        w = self.w
        e = w.ready_enrollment(w.stu[0], w.course_plain)
        a = w.s.open_assessment(w.admin["id"], e["id"], gap_threshold=10)
        w.s.add_score(w.ex[0]["id"], a["id"], w.ex[0]["id"], 99)
        w.s.add_score(w.ex[1]["id"], a["id"], w.ex[1]["id"], 20)
        rid = w.s.get_assessment(a["id"])["children"][0]["id"]
        w.s.add_score(w.ex[0]["id"], rid, w.ex[0]["id"], 70)
        w.s.add_score(w.ex[1]["id"], rid, w.ex[1]["id"], 72)
        head = w.s.get_assessment(a["id"])
        leaf = w.s.get_assessment(rid)
        self.assertEqual(head["status"], "in_review")
        self.assertFalse(head["result_locked"])
        self.assertEqual(leaf["status"], "open")
