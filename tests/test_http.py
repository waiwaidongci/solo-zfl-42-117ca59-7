"""HTTP 层测试：起真实 TCP 服务器，覆盖鉴权、幂等、重复提交、
并发评定/报名/派工，以及重启后的一致性。"""
from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from app.db import Database
from app.server import make_server
from tests.helpers import World


def _env():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    db = Database(path)
    world = World(db, path)
    httpd = make_server("127.0.0.1", 0, db, run_seed=False)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return world, db, httpd, path, port


class ApiClient:
    def __init__(self, port):
        self.port = port

    def call(self, method, path, body=None, user=None, idem=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if user is not None:
            headers["X-User-Id"] = user
        if idem is not None:
            headers["Idempotency-Key"] = idem
        payload = json.dumps(body) if body is not None else None
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        conn.close()
        data = json.loads(raw) if raw else None
        return resp.status, data


class HttpTestCase(unittest.TestCase):
    """每个用例独立起一套服务器 + 文件库，避免用例间对同一学员/工位争抢。"""

    def setUp(self):
        self.world, self.db, self.httpd, self.path, self.port = _env()
        self.api = ApiClient(self.port)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.db.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + ext)
            except OSError:
                pass

    # 便捷
    def admin(self): return self.world.admin["id"]
    def ex(self, i): return self.world.ex[i]["id"]
    def stu(self, i): return self.world.stu[i]["id"]

    def ready(self, student, course, **kw):
        return self.world.ready_enrollment(student, course, **kw)

    # ----------------------------------------------------------- 鉴权
    def test_missing_and_wrong_role_forbidden(self):
        st, _ = self.api.call("POST", "/api/skills", {"name": "无身份技能"})
        self.assertEqual(st, 403)
        st, body = self.api.call("POST", "/api/courses",
                                 {"name": "x", "skill_id": self.world.skill_plain["id"],
                                  "required_hours": 1, "required_practice": 1,
                                  "allowed_absences": 0}, user=self.stu(0))
        self.assertEqual(st, 403)
        self.assertEqual(body["error"]["code"], "forbidden")

    def test_score_endpoint_enforces_examiner_identity(self):
        e = self.ready(self.world.stu[0], self.world.course_plain)
        st, a = self.api.call("POST", "/api/assessments",
                              {"enrollment_id": e["id"]}, user=self.admin())
        self.assertEqual(st, 201)
        # 学员评分
        st, _ = self.api.call("POST", f"/api/assessments/{a['id']}/scores",
                              {"examiner_id": self.stu(0), "score": 90}, user=self.stu(0))
        self.assertEqual(st, 403)
        # 代评：考评员0 提交考评员1 的分
        st, _ = self.api.call("POST", f"/api/assessments/{a['id']}/scores",
                              {"examiner_id": self.ex(1), "score": 90}, user=self.ex(0))
        self.assertEqual(st, 403)

    # ----------------------------------------------------------- 幂等 / 重复提交
    def test_idempotency_key_replays_same_result(self):
        key = "fixed-key-create-skill"
        st1, b1 = self.api.call("POST", "/api/skills", {"name": "幂等技能X"},
                                user=self.admin(), idem=key)
        st2, b2 = self.api.call("POST", "/api/skills", {"name": "幂等技能X"},
                                user=self.admin(), idem=key)
        self.assertEqual((st1, st2), (201, 201))
        self.assertEqual(b1["id"], b2["id"])
        st, skills = self.api.call("GET", "/api/skills")
        self.assertEqual(sum(1 for s in skills if s["name"] == "幂等技能X"), 1)

    def test_idempotency_replays_error_too(self):
        # 门控失败的请求也应被缓存：重复提交返回同样的 422，而不会推进任何状态。
        e = self.world.s.create_enrollment(self.admin(), self.world.stu[1]["id"],
                                           self.world.course_plain["id"])
        self.world.s.start_learning(self.admin(), e["id"])
        body = {}
        st1, b1 = self.api.call("POST", f"/api/enrollments/{e['id']}/advance", body,
                                user=self.admin(), idem="advance-fail-1")
        st2, b2 = self.api.call("POST", f"/api/enrollments/{e['id']}/advance", body,
                                user=self.admin(), idem="advance-fail-1")
        self.assertEqual((st1, st2), (422, 422))
        self.assertEqual(b1["error"]["code"], b2["error"]["code"])

    def test_duplicate_submit_without_idem_key_conflicts(self):
        # 不带幂等键的重复报名 → 第二次 409（数据库唯一约束兜底，事务回滚）。
        st1, _ = self.api.call("POST", "/api/enrollments",
                               {"student_id": self.stu(2),
                                "course_id": self.world.course_plain["id"]},
                               user=self.admin())
        self.assertEqual(st1, 201)
        st2, body = self.api.call("POST", "/api/enrollments",
                                  {"student_id": self.stu(2),
                                   "course_id": self.world.course_plain["id"]},
                                  user=self.admin())
        self.assertEqual(st2, 409)
        self.assertEqual(body["error"]["code"], "duplicate_enrollment")

    # ----------------------------------------------------------- 并发评定
    def test_concurrent_scores_only_two_accepted(self):
        e = self.ready(self.world.stu[0], self.world.course_plain)
        _, a = self.api.call("POST", "/api/assessments",
                             {"enrollment_id": e["id"]}, user=self.admin())
        barrier = threading.Barrier(4)

        def score(i):
            barrier.wait()
            return self.api.call("POST", f"/api/assessments/{a['id']}/scores",
                                 {"examiner_id": self.ex(i), "score": 80 + i},
                                 user=self.ex(i))

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(score, range(4)))
        statuses = sorted(s for s, _ in results)
        # 恰好 2 个 201（自动评定后锁定），其余 409。
        self.assertEqual(statuses.count(201), 2)
        self.assertTrue(all(s in (409,) for s in statuses[2:]))
        st, detail = self.api.call("GET", f"/api/assessments/{a['id']}")
        self.assertEqual(detail["status"], "passed")
        self.assertEqual(len(detail["scores"]), 2)

    def test_concurrent_scores_race_at_exactly_required_count(self):
        # required=3，三名考评员同时提交最后一分：恰好 3 个成功，不重不漏。
        e = self.ready(self.world.stu[1], self.world.course_plain)
        _, a = self.api.call("POST", "/api/assessments",
                             {"enrollment_id": e["id"], "examiner_required": 3},
                             user=self.admin())
        # 先放 2 分
        self.api.call("POST", f"/api/assessments/{a['id']}/scores",
                      {"examiner_id": self.ex(0), "score": 70}, user=self.ex(0))
        self.api.call("POST", f"/api/assessments/{a['id']}/scores",
                      {"examiner_id": self.ex(1), "score": 72}, user=self.ex(1))
        barrier = threading.Barrier(2)

        def last(i):
            barrier.wait()
            return self.api.call("POST", f"/api/assessments/{a['id']}/scores",
                                 {"examiner_id": self.ex(i), "score": 74}, user=self.ex(i))

        # ex2 与一个“迟到的重复 ex0”竞争
        with ThreadPoolExecutor(max_workers=2) as pool:
            r = list(pool.map(last, (2, 3)))
        codes = sorted(s for s, _ in r)
        self.assertEqual(codes[0], 201)
        self.assertEqual(codes[1], 409)
        _, detail = self.api.call("GET", f"/api/assessments/{a['id']}")
        self.assertEqual(len(detail["scores"]), 3)
        self.assertEqual(detail["status"], "passed")

    # ----------------------------------------------------------- 并发报名 / 派工
    def test_concurrent_duplicate_enrollment_single_winner(self):
        sid, cid = self.stu(2), self.world.course_plain["id"]
        # 该学员尚未报这门课；并发两次，只有一个 201。
        barrier = threading.Barrier(2)

        def enroll(_):
            barrier.wait()
            return self.api.call("POST", "/api/enrollments",
                                 {"student_id": sid, "course_id": cid},
                                 user=self.admin())

        with ThreadPoolExecutor(max_workers=2) as pool:
            res = list(pool.map(enroll, range(2)))
        codes = sorted(s for s, _ in res)
        self.assertEqual(codes, [201, 409])

    def test_concurrent_key_assignment_single_winner(self):
        # 两名都合格的学员并发抢同一关键工位，只允许一人在岗。
        self.world.qualify_streak(self.world.stu[0], self.world.course_key,
                                  scores_per_round=((80, 82), (85, 87)))
        self.world.qualify_streak(self.world.stu[1], self.world.course_key,
                                  scores_per_round=((80, 82), (85, 87)))
        ws = self.world.ws_key2["id"]
        barrier = threading.Barrier(2)

        def assign(i):
            barrier.wait()
            return self.api.call("POST", "/api/assignments",
                                 {"student_id": self.stu(i), "workstation_id": ws},
                                 user=self.admin())

        with ThreadPoolExecutor(max_workers=2) as pool:
            res = list(pool.map(assign, (0, 1)))
        codes = sorted(s for s, _ in res)
        self.assertEqual(codes, [201, 409])
        st, jobs = self.api.call("GET", "/api/assignments")
        active = [j for j in jobs if j["status"] == "active" and j["workstation_id"] == ws]
        self.assertEqual(len(active), 1)

    def test_concurrent_same_idempotency_key_single_execution(self):
        # 同一个幂等键并发提交，服务端占位保证只创建一条。
        barrier = threading.Barrier(3)

        def create(_):
            barrier.wait()
            return self.api.call("POST", "/api/skills", {"name": "并发幂等技能"},
                                 user=self.admin(), idem="concurrent-idem-1")

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(create, range(3)))
        ids = {b["id"] for _, b in results}
        self.assertEqual(len(ids), 1)
        self.assertTrue(all(s == 201 for s, _ in results))
        st, skills = self.api.call("GET", "/api/skills")
        self.assertEqual(sum(1 for s in skills if s["name"] == "并发幂等技能"), 1)

    # ----------------------------------------------------------- 门控与回滚（HTTP 维度）
    def test_gate_failure_returns_details_and_no_mutation(self):
        e = self.world.s.create_enrollment(self.admin(), self.world.stu[2]["id"],
                                           self.world.course_key["id"])
        self.world.s.start_learning(self.admin(), e["id"])
        st, body = self.api.call("POST", f"/api/enrollments/{e['id']}/advance",
                                 {}, user=self.admin())
        self.assertEqual(st, 422)
        self.assertFalse(body["error"]["details"]["ready"])
        st2, enr = self.api.call("GET", f"/api/enrollments/{e['id']}")
        self.assertEqual(enr["stage"], "learning")

    # ----------------------------------------------------------- 端到端正向
    def test_end_to_end_happy_path(self):
        e = self.ready(self.world.stu[2], self.world.course_key)
        # 连续两次通过
        for idx, kind in enumerate(("initial", "retake")):
            _, a = self.api.call("POST", "/api/assessments",
                                 {"enrollment_id": e["id"], "kind": kind},
                                 user=self.admin())
            st, _ = self.api.call("POST", f"/api/assessments/{a['id']}/scores",
                                  {"examiner_id": self.ex(0), "score": 80 + idx},
                                  user=self.ex(0))
            self.assertEqual(st, 201)
            st, _ = self.api.call("POST", f"/api/assessments/{a['id']}/scores",
                                  {"examiner_id": self.ex(1), "score": 82 + idx},
                                  user=self.ex(1))
            self.assertEqual(st, 201)
            _, detail = self.api.call("GET", f"/api/assessments/{a['id']}")
            self.assertEqual(detail["status"], "passed")
        # 发证
        st, q = self.api.call("POST", "/api/certificates",
                              {"enrollment_id": e["id"], "valid_months": 12},
                              user=self.admin())
        self.assertEqual(st, 201)
        # 派关键工位
        st, j = self.api.call("POST", "/api/assignments",
                              {"student_id": self.stu(2),
                               "workstation_id": self.world.ws_key2["id"]},
                              user=self.admin())
        self.assertEqual(st, 201)
        self.assertEqual(j["status"], "active")
        # 撤销 → 派工立即停止
        st, _ = self.api.call("POST", f"/api/qualifications/{q['id']}/revoke",
                              {"reason": "年审不过"}, user=self.admin())
        self.assertEqual(st, 200)
        _, jobs = self.api.call("GET", "/api/assignments")
        ended = [x for x in jobs if x["id"] == j["id"]][0]
        self.assertEqual(ended["status"], "ended")
        # 再派被拒
        st, body = self.api.call("POST", "/api/assignments",
                                 {"student_id": self.stu(2),
                                  "workstation_id": self.world.ws_key["id"]},
                                 user=self.admin())
        self.assertEqual(st, 422)
        self.assertEqual(body["error"]["code"], "no_valid_qualification")


class RestartPersistenceTest(unittest.TestCase):
    def test_state_and_expiry_sweep_survive_restart(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)

        def boot():
            db = Database(path)
            world = World(db, path)
            httpd = make_server("127.0.0.1", 0, db, run_seed=False)
            port = httpd.server_address[1]
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            return world, db, httpd, port

        world1, db1, httpd1, port1 = boot()
        # 造一名关键工序合格并发证、在岗的学员
        e, q, _ = world1.qualify_streak(
            world1.stu[0], world1.course_key,
            scores_per_round=((80, 82), (85, 87)))
        j = world1.s.assign_workstation(world1.admin["id"], world1.stu[0]["id"],
                                        world1.ws_key["id"])
        qid, jid, sid, wskey = q["id"], j["id"], world1.stu[0]["id"], world1.ws_key["id"]
        # 直接把有效期改到过去，但“不”立即清扫——模拟服务停机期间资格到期。
        with db1.transaction() as conn:
            conn.execute(
                "UPDATE qualifications SET valid_until='2020-01-01T00:00:00Z' WHERE id=?",
                (qid,))
        # 关闭第一个实例（模拟重启）
        httpd1.shutdown(); httpd1.server_close(); db1.close()

        # 重新打开同一文件：启动清扫过期资格并停止派工
        db2 = Database(path)
        from app.services import Service
        svc2 = Service(db2)
        n = svc2.expire_qualifications()  # 与 make_server 启动钩子相同的动作
        self.assertGreaterEqual(n, 1)
        httpd2 = make_server("127.0.0.1", 0, db2, run_seed=False)
        port2 = httpd2.server_address[1]
        threading.Thread(target=httpd2.serve_forever, daemon=True).start()
        api = ApiClient(port2)

        try:
            # 数据仍在
            st, quals = api.call("GET", "/api/qualifications")
            self.assertTrue(any(x["id"] == qid and x["status"] == "expired"
                                for x in quals))
            st, jobs = api.call("GET", "/api/assignments")
            job = [x for x in jobs if x["id"] == jid][0]
            self.assertEqual(job["status"], "ended")
            self.assertIn("过期", job["end_reason"])
            # 过期后不能再派关键工序
            st, body = api.call("POST", "/api/assignments",
                                {"student_id": sid, "workstation_id": wskey},
                                user=world1.admin["id"])
            self.assertEqual(st, 422)
            self.assertEqual(body["error"]["code"], "no_valid_qualification")
        finally:
            httpd2.shutdown(); httpd2.server_close(); db2.close()
            for ext in ("", "-wal", "-shm"):
                try:
                    os.remove(path + ext)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
