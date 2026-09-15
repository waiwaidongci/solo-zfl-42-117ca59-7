# 漆线雕学徒培训与上岗资格台

面向漆线雕工坊的**学徒培训与上岗资格管理系统**：管理员建立课程、工位与技能项；学员报名后按
**课时 / 实操 / 考核**推进，缺课或未达标不能进入下一阶段；考核由**至少两名考评员**独立评分，
评分差距过大自动进入**复评**；连续合格且证书在有效期内才允许分配**关键工序**；资格
过期 / 暂停 / 撤销后**立即停止派工**。服务重启后记录保持一致，同一学员同一技能只保留一份有效资格。

仅依赖 **Python 3.10+ 标准库 + SQLite(WAL)**，无第三方依赖、无构建步骤。

---

## 快速开始

```bash
python3 run.py                       # 默认 0.0.0.0:8000，数据文件 data/app.db，并写入演示种子
python3 run.py --port 8000 --db data/app.db --no-seed
```

打开 `http://localhost:8000/`，右上角切换身份（管理员 / 考评员 / 学员），依次操作
**① 建课 → ② 报名与学习 → ③ 考核与复评 → ④ 发证与资格 → ⑤ 派工**。

种子数据含 1 名管理员、3 名考评员、2 名学员、2 门课、4 个工位；其中「周阿竹」已达标进入考核阶段，
可直接演示考核—发证—派工。

## 自动化测试

```bash
python3 -m unittest discover -s tests -v
```

51 个用例，覆盖：阶段门控、评分/复评/越权/重复、发证唯一资格、资格生命周期与即时停派工、
事务回滚，以及**真实 TCP 下的并发评定 / 并发报名 / 并发派工 / 并发幂等键**和**重启一致性**。
并发用例使用临时文件（WAL）库——SQLite 共享内存库是表级锁、不响应 `busy_timeout`，
只有文件 WAL 才与生产并发行为一致。

---

## 业务规则与落点

| 规则 | 实现位置 | 违反时 |
| --- | --- | --- |
| 建课程/工位/技能、报名、发证、派工仅管理员 | `services.py` 各写方法 `_require_role` | `403 forbidden` |
| 累计课时、合格实操、缺课次数三项同时达标才能进考核 | `gate_status` + `advance_to_assessment` | `422 gate_not_met`（带各项实际值/阈值） |
| 缺课仅学习阶段记录，超过课程允许上限即不达标 | `add_attendance` | 门控拦截 |
| 同一学员同一课程不能重复报名 | `enrollments` 表 `UNIQUE` + 服务层预检 | `409 duplicate_enrollment` |
| 考核至少 2 名考评员，各自独立打分 | `examiner_required`，`assessment_scores UNIQUE(assessment,examiner)` | 不足 `422 examiners_insufficient` |
| 考评员只能提交本人评分；学员/管理员不能评分；禁止代评、自评 | `add_score` 身份比对 | `403 forbidden` |
| 同一考评员重复评分、对已锁定单补分 | 唯一约束 + `result_locked/status` 检查 | `409 duplicate_score / assessment_locked` |
| 最高分与最低分差 > 阈值 → 自动复评（复评需 ≥3 名考评员） | `_evaluate` 建复评子单，链头置 `in_review` | 链头评分被拒 `assessment_locked` |
| 重复考核（已有进行中考核再开单） | 部分唯一索引 `uq_open_chain` + 预检 | `409 assessment_open` |
| 复评结论回写链头，叶子只计一次（连续合格不被链头重复累计） | `_evaluate` 同步父单；`consecutive_passes` 只数叶子 | — |
| 必须有通过的终态叶子考核才能发证，且同一考核不能重复发证 | `issue_certificate` + `qualifications.assessment_id UNIQUE` | `422 no_passing_assessment` / `409 duplicate_certificate` |
| **同一学员同一技能只保留一份有效资格** | 部分唯一索引 `uq_valid_qual`；重发作废旧证(supersede) | 暂停中重发 `409 qual_suspended` |
| 关键工序：资格 active 且未过期 + 连续合格达标 | `assign_workstation` 校验资格、有效期、`min_consecutive` | `422 no_valid_qualification / qual_expired / streak_insufficient` |
| 资格暂停 / 撤销 / 过期 → 立即停止其在岗派工 | `suspend/revoke` + `_stop_assignments`；`expire_qualifications` | 派工同事务置 `ended` |
| 同一工位同时仅一人、同一学员同时仅一处在岗 | 部分唯一索引 `uq_station_active / uq_student_active` | `409 workstation_busy / student_busy` |
| 重启后一致：启动即清扫到期资格并停派工 | `make_server` 启动调用 `expire_qualifications` | 到期资格自动 `expired` |
| 任何写操作中途失败整体回滚 | 所有写方法包在单个 `BEGIN IMMEDIATE` 事务 | 无半成品状态，审计也不脏写 |

> 连续合格按**终态叶子考核**的发生顺序（以 SQLite 单调 `rowid` 排序，规避秒级时间戳并列与随机 ID）
> 从最近一次向前数，遇到一次「不通过」即清零。

---

## HTTP 接口

所有写接口：`POST` + JSON；鉴权用请求头 `X-User-Id` 指定当前用户；
可带 `Idempotency-Key` 实现安全重试（同键返回**同一结果**，错误结果同样缓存；并发同键只执行一次）。
错误统一为 `{"error":{"code","message"[, "details"]}}`。

| 方法 & 路径 | 角色 | 说明 |
| --- | --- | --- |
| `POST /api/users` `/api/skills` `/api/courses` `/api/workstations` | 管理员 | 人员 / 技能 / 课程（含门控阈值）/ 工位 |
| `POST /api/enrollments` | 管理员 | 报名 `{student_id,course_id}` |
| `POST /api/enrollments/{id}/start` | 管理员 | 进入学习 |
| `POST /api/enrollments/{id}/attendance` | 管理员 | 记课时/缺课 `{lesson_date,present,hours}` |
| `POST /api/enrollments/{id}/practicals` | 管理员 | 记实操 `{title,passed}` |
| `POST /api/enrollments/{id}/advance` | 管理员 | 门控通过后进入考核阶段 |
| `GET  /api/enrollments/{id}` | 任意 | 门控明细、考勤、实操、考核、连续合格次数 |
| `POST /api/assessments` | 管理员/考评员 | 开考核单（可配合格线/分差阈值/考评员数） |
| `POST /api/assessments/{id}/scores` | **考评员本人** | `{examiner_id,score}`；凑齐人数自动评定 |
| `POST /api/assessments/{id}/finalize` | 管理员 | 手动评定收口（人数不足/复评中拒绝） |
| `POST /api/assessments/{id}/review` | 管理员 | 手动补开复评 |
| `GET  /api/assessments/{id}` | 任意 | 评分、分差、复评子单 |
| `POST /api/certificates` | 管理员 | 发证 `{enrollment_id,valid_months}` |
| `POST /api/qualifications/{id}/suspend|revoke|restore` | 管理员 | 暂停 / 撤销 / 恢复 |
| `POST /api/assignments` | 管理员 | 派工 `{student_id,workstation_id}` |
| `POST /api/assignments/{id}/end` | 管理员 | 结束派工 |
| `GET  /api/{users,skills,courses,workstations,enrollments,assessments,qualifications,assignments,audit}` | 任意 | 列表 / 台账 / 审计 |
| `POST /api/dev/qualifications/{id}/expire` | 管理员 | 演练辅助：把资格置为到期（等价真实到期，立即停派工） |

### 接口如何覆盖题目要求的四类异常

- **重复提交**：业务唯一约束（重复报名/重复评分/重复发证/重复考核）返回 `409`；
  网络重试建议带 `Idempotency-Key`，同键只落一次库。
- **并发评定**：所有评分在 `BEGIN IMMEDIATE` 事务内串行；凑齐考评员数的瞬间由
  `result_locked` + 唯一索引保证“恰齐 N 人、不重不漏”，迟到评分 `409`。
- **越权**：角色 + 身份比对（代评/自评/非考评员评分）返回 `403`。
- **回滚**：事务内任何断言或约束异常都 `ROLLBACK`（见 `test_*rollback*`）。

---

## 数据模型（SQLite，见 `app/db.py`）

```
users  skills  courses  workstations
enrollments ──< attendance / practicals
enrollments ──< assessments ──< assessment_scores
                   │ parent_id（复评链：链头 → 复评叶子）
qualifications（active/suspended/revoked/expired，部分唯一索引保证每学员每技能仅一份有效）
assignments（active/ended，工位与在岗各一条部分唯一索引）
audit_log（全部关键操作）  idempotency（幂等键）
```

关键部分唯一索引：`uq_open_chain`、`uq_valid_qual`、`uq_station_active`、`uq_student_active`。
文件库开启 `PRAGMA journal_mode=WAL`、`foreign_keys=ON`、`busy_timeout=5000`。

## 目录

```
run.py              启动入口
app/db.py           连接、WAL、schema 与部分唯一索引、事务封装
app/services.py     全部业务规则（门控/考核/复评/发证/资格/派工/审计）
app/server.py       HTTP 路由、角色鉴权、幂等中间件、静态托管
app/seed.py         演示数据
app/util.py         UTC 时间与证书有效期
web/                原生 HTML/CSS/JS 前端（五个页签覆盖七环节）
tests/              51 个自动化测试（服务层 + 真实 TCP 并发 + 重启一致性）
```
