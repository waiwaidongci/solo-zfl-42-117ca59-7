"""测试公共夹具：在临时文件（WAL）数据库上构造一套可复用的培训世界。

之所以不用内存库：SQLite 共享缓存内存库是表级锁、不响应 busy_timeout，
无法真实模拟“并发评定排队提交”。文件 WAL 库才与生产一致。
"""
from __future__ import annotations

import os
import tempfile
import unittest

from app.db import Database
from app.services import Service


class World:
    def __init__(self, db: Database, path: str):
        self.db = db
        self.path = path
        self.s = Service(db)
        self.admin = self.s.create_user(None, "管理员", "admin", system=True)
        self.ex = [self.s.create_user(None, f"考评员{i}", "examiner", system=True)
                   for i in range(1, 5)]  # 4 名考评员
        self.stu = [self.s.create_user(None, f"学徒{i}", "student", system=True)
                    for i in range(1, 4)]  # 3 名学员

        self.skill_key = self.s.create_skill(self.admin["id"], "上金粉（关键）", True, 2)
        self.skill_plain = self.s.create_skill(self.admin["id"], "盘线", False, 1)

        self.course_key = self.s.create_course(
            self.admin["id"], "金粉课", self.skill_key["id"], 24, 6, 2)
        self.course_plain = self.s.create_course(
            self.admin["id"], "盘线课", self.skill_plain["id"], 8, 2, 1)

        self.ws_key = self.s.create_workstation(
            self.admin["id"], "金粉关键工位", self.skill_key["id"], True)
        self.ws_key2 = self.s.create_workstation(
            self.admin["id"], "金粉关键工位二", self.skill_key["id"], True)
        self.ws_plain = self.s.create_workstation(
            self.admin["id"], "普通盘线工位", self.skill_plain["id"], False)

    # --- 便捷构造器 ---
    def ready_enrollment(self, student, course, *, hours=24, practice=6, absences=0):
        """报名 → 学习，并补齐课时/实操/缺课 → 进入考核阶段。"""
        e = self.s.create_enrollment(self.admin["id"], student["id"], course["id"])
        self.s.start_learning(self.admin["id"], e["id"])
        present_sessions = 0
        remaining_h = hours
        # 用 8 课时/次的出勤凑够课时
        while remaining_h > 0:
            h = min(8, remaining_h)
            self.s.add_attendance(self.admin["id"], e["id"], "2026-08-01", True, h)
            remaining_h -= h
            present_sessions += 1
        for i in range(absences):
            self.s.add_attendance(self.admin["id"], e["id"], f"2026-07-{i+1:02d}", False, 0)
        for i in range(practice):
            self.s.add_practice(self.admin["id"], e["id"], f"实操{i+1}", True)
        self.s.advance_to_assessment(self.admin["id"], e["id"])
        return self.s.get_enrollment(e["id"])

    def assess(self, enrollment_id, scores, *, kind="initial", required=2, gap=15, pass_score=60):
        """开考核单并由前 N 名考评员打分，返回考核详情。"""
        a = self.s.open_assessment(
            self.admin["id"], enrollment_id, kind=kind,
            pass_score=pass_score, gap_threshold=gap, examiner_required=required)
        for i, sc in enumerate(scores):
            self.s.add_score(self.ex[i]["id"], a["id"], self.ex[i]["id"], sc)
        return self.s.get_assessment(a["id"])

    def certify(self, enrollment_id, months=12):
        return self.s.issue_certificate(self.admin["id"], enrollment_id, months)

    def qualify_streak(self, student, course, scores_per_round=(80, 82), months=12):
        """让学员达到“连续合格 N 次”并发证，返回 (enrollment, qualification)。"""
        e = self.ready_enrollment(student, course)
        rounds = []
        for idx, sc in enumerate(scores_per_round):
            r = self.assess(e["id"], sc, kind="initial" if idx == 0 else "retake")
            rounds.append(r)
        q = self.certify(e["id"], months)
        return e, q, rounds

    def cleanup(self):
        self.db.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + ext)
            except OSError:
                pass


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)  # Database 自己创建
        self.db = Database(path)
        self.path = path
        self.w = World(self.db, path)

    def tearDown(self):
        self.w.cleanup()
