"""开发用种子数据：管理员 / 考评员 / 学员、技能、课程、工位，
以及一名“待考核”学员（课时、实操、考勤均达标），方便直接演示。"""
from __future__ import annotations

from .db import Database
from .services import Service


def seed(db: Database) -> None:
    s = Service(db)
    with db.transaction() as conn:
        if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]:
            return

    admin = s.create_user(None, "工坊管理员", "admin", system=True)
    ex1 = s.create_user(None, "陈师傅（考评员）", "examiner", system=True)
    ex2 = s.create_user(None, "林师傅（考评员）", "examiner", system=True)
    ex3 = s.create_user(None, "吴师傅（考评员）", "examiner", system=True)
    stu1 = s.create_user(None, "周阿竹（学徒）", "student", system=True)
    stu2 = s.create_user(None, "李阿土（学徒）", "student", system=True)

    # 关键工序技能：上金粉（连续合格 2 次方可独立承担）。
    gold = s.create_skill(admin["id"], "上金粉（关键工序）", True, 2)
    # 一般技能：盘线打底。
    thread = s.create_skill(admin["id"], "盘线打底", False, 1)

    c_gold = s.create_course(admin["id"], "上金粉技艺", gold["id"], 24, 6, 2)
    c_thread = s.create_course(admin["id"], "盘线基础", thread["id"], 16, 4, 2)

    s.create_workstation(admin["id"], "金粉关键工位 A", gold["id"], True)
    s.create_workstation(admin["id"], "金粉关键工位 B", gold["id"], True)
    s.create_workstation(admin["id"], "盘线练习工位", thread["id"], False)
    s.create_workstation(admin["id"], "打磨辅助工位", None, False)

    # 周阿竹：报名 -> 学习 -> 课时/实操/考勤达标 -> 进入考核，尚未开考。
    e1 = s.create_enrollment(admin["id"], stu1["id"], c_gold["id"])
    s.start_learning(admin["id"], e1["id"])
    for i in range(3):
        s.add_attendance(admin["id"], e1["id"], f"2026-08-{10 + i:02d}", True, 8)
    for t in ("缠枝莲金粉", "海水江崖金粉", "八宝纹金粉", "花鸟金粉",
              "卷草纹金粉", "云雷纹金粉"):
        s.add_practice(admin["id"], e1["id"], t, True)
    s.advance_to_assessment(admin["id"], e1["id"])

    # 李阿土：报名进入学习但缺课超上限，门控不通过（用于演示被拦）。
    e2 = s.create_enrollment(admin["id"], stu2["id"], c_thread["id"])
    s.start_learning(admin["id"], e2["id"])
    s.add_attendance(admin["id"], e2["id"], "2026-08-11", True, 8)
    s.add_attendance(admin["id"], e2["id"], "2026-08-12", False, 0)
    s.add_attendance(admin["id"], e2["id"], "2026-08-13", False, 0)
    s.add_attendance(admin["id"], e2["id"], "2026-08-14", False, 0)
    s.add_practice(admin["id"], e2["id"], "基础盘线", True)
