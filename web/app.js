/* 漆线雕资格台前端：原生 JS，无构建依赖。 */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (iso) => iso ? iso.replace("T", " ").replace("Z", " UTC") : "—";

  const state = { me: null, data: {} };

  // --------------------------------------------------------------- API
  async function api(method, path, body, { idempotent = false } = {}) {
    const headers = { "Content-Type": "application/json" };
    if (state.me) headers["X-User-Id"] = state.me.id;
    if (method !== "GET" && idempotent) headers["Idempotency-Key"] = crypto.randomUUID();
    const res = await fetch(path, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });
    let payload = null;
    try { payload = await res.json(); } catch { /* 空体 */ }
    if (!res.ok) {
      const e = payload?.error || {};
      const err = new Error(e.message || `请求失败 ${res.status}`);
      err.status = res.status;
      err.code = e.code;
      err.details = e.details;
      throw err;
    }
    return payload;
  }

  async function loadAll() {
    const [users, skills, courses, workstations, enrollments,
      assessments, qualifications, assignments, audit] = await Promise.all([
      api("GET", "/api/users"), api("GET", "/api/skills"),
      api("GET", "/api/courses"), api("GET", "/api/workstations"),
      api("GET", "/api/enrollments"), api("GET", "/api/assessments"),
      api("GET", "/api/qualifications"), api("GET", "/api/assignments"),
      api("GET", "/api/audit"),
    ]);
    Object.assign(state.data, { users, skills, courses, workstations,
      enrollments, assessments, qualifications, assignments, audit });
    // 详情（报名、考核、资格）需要级联字段，逐个拉取。
    state.data.enrDetail = await mapAsync(enrollments, (e) => api("GET", `/api/enrollments/${e.id}`));
    state.data.asmDetail = await mapAsync(assessments, (a) => api("GET", `/api/assessments/${a.id}`));
    state.data.qualDetail = await mapAsync(qualifications, (q) => api("GET", `/api/qualifications/${q.id}`));
  }
  const mapAsync = async (arr, fn) => Promise.all(arr.map(fn));

  // --------------------------------------------------------------- 通用 UI
  let toastTimer = null;
  function toast(msg, kind = "info") {
    const el = $("#toast");
    el.className = `toast ${kind}`;
    el.textContent = msg;
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 4200);
  }
  function errText(e) {
    let t = `[${e.status || ""} ${e.code || ""}] ${e.message}`;
    if (e.details) t += "\n" + JSON.stringify(e.details, null, 1);
    return t;
  }

  function tag(label, cls) { return `<span class="tag ${cls}">${esc(label)}</span>`; }
  const stageTag = (s) => ({
    enrolled: tag("已报名", "gray"), learning: tag("学习中", "blue"),
    assessment: tag("考核阶段", "amber"), certified: tag("已认证", "green"),
  }[s] || esc(s));
  const asmTag = (s) => ({
    open: tag("待评分", "blue"), in_review: tag("复评中", "violet"),
    passed: tag("通过", "green"), failed: tag("不通过", "red"),
  }[s] || esc(s));
  const qualTag = (s) => ({
    active: tag("有效", "green"), suspended: tag("已暂停", "amber"),
    revoked: tag("已撤销", "red"), expired: tag("已过期", "gray"),
  }[s] || esc(s));
  const kindLabel = (k) => ({ initial: "初考", retake: "重考", review: "复评" }[k] || k);

  function nameOf(list, id) {
    const x = (list || []).find((i) => i.id === id);
    return x ? x.name : id;
  }

  // 填充 <select data-source>
  function fillSelects() {
    const sources = {
      skills: state.data.skills.map((s) => ({
        value: s.id, label: `${s.name}${s.key_process ? " ★关键" : ""}` })),
      courses: state.data.courses.map((c) => {
        const sk = state.data.skills.find((s) => s.id === c.skill_id);
        return { value: c.id, label: `${c.name}（${sk ? sk.name : ""}）` };
      }),
      workstations: state.data.workstations.map((w) => ({
        value: w.id, label: `${w.name}${w.is_key ? " ★" : ""}` })),
      students: state.data.users.filter((u) => u.role === "student")
        .map((u) => ({ value: u.id, label: u.name })),
      enrollmentsAssessable: assessableEnrollments().map((e) => ({ value: e.id, label: e.label })),
      enrollmentsCertifiable: certifiableEnrollments().map((e) => ({ value: e.id, label: e.label })),
    };
    $$("select[data-source]").forEach((sel) => {
      const rows = sources[sel.dataset.source] || [];
      const prev = sel.value;
      const empty = sel.dataset.allowempty ? `<option value="">（不绑定）</option>` : "";
      sel.innerHTML = empty + rows.map((r) =>
        `<option value="${esc(r.value)}">${esc(r.label)}</option>`).join("");
      if (prev && rows.some((r) => r.value === prev)) sel.value = prev;
    });
  }

  function assessableEnrollments() {
    return state.data.enrDetail
      .filter((e) => e.stage === "assessment" || e.stage === "certified")
      .map((e) => ({
        id: e.id,
        label: `${e.student.name} · ${e.course.name}（已连续合格 ${e.consecutive_passes}）`,
      }));
  }
  function certifiableEnrollments() {
    // 至少有一张通过的叶子终态考核
    return state.data.enrDetail
      .filter((e) => (e.assessments || []).some((a) =>
        a.status === "passed" && a.result_locked &&
        !(state.data.asmDetail.find((d) => d.id === a.id)?.children || []).length))
      .map((e) => ({
        id: e.id,
        label: `${e.student.name} · ${e.course.name}（连续合格 ${e.consecutive_passes}）`,
      }));
  }

  // --------------------------------------------------------------- 渲染：基础
  function renderBase() {
    renderSkills(); renderCourses(); renderStations();
  }
  function renderSkills() {
    $('[data-list="skills"]').innerHTML = state.data.skills.map((s) => `
      <div class="row ${s.key_process ? "key" : ""}">
        <h4>${esc(s.name)} ${s.key_process ? tag("关键工序", "red") : tag("一般技能", "gray")}</h4>
        <div class="meta">连续合格要求：${s.min_consecutive} 次</div>
      </div>`).join("") || empty();
  }
  function renderCourses() {
    $('[data-list="courses"]').innerHTML = state.data.courses.map((c) => {
      const sk = state.data.skills.find((s) => s.id === c.skill_id);
      return `<div class="row">
        <h4>${esc(c.name)}</h4>
        <div class="meta">技能：${esc(sk ? sk.name : c.skill_id)}<br>
        门控：累计课时 ≥ ${c.required_hours}，合格实操 ≥ ${c.required_practice}，缺课 ≤ ${c.allowed_absences}</div>
      </div>`;
    }).join("") || empty();
  }
  function renderStations() {
    $('[data-list="workstations"]').innerHTML = state.data.workstations.map((w) => `
      <div class="row ${w.is_key ? "key" : ""}">
        <h4>${esc(w.name)} ${w.is_key ? tag("关键工位", "red") : tag("普通工位", "gray")}</h4>
        <div class="meta">绑定技能：${w.skill_id ? esc(nameOf(state.data.skills, w.skill_id)) : "无"}</div>
      </div>`).join("") || empty();
  }

  // --------------------------------------------------------------- 渲染：报名与学习
  function gateHtml(gate) {
    const c = gate.checks;
    const item = (ok, t, a, n, suffix = "≥") =>
      `<div class="g ${ok ? "ok" : "no"}">${t}: ${suffix}${n}｜实际 ${a} ${ok ? "✓" : "✗"}</div>`;
    return `<div class="gate">
      ${item(c.hours.ok, "课时", c.hours.actual, c.hours.need)}
      ${item(c.practice.ok, "实操", c.practice.actual, c.practice.need)}
      ${item(c.absences.ok, "缺课", c.absences.actual, c.absences.need, "≤")}
    </div>`;
  }

  function renderEnrollments() {
    const isAdmin = meIs("admin");
    $("#enrollmentProgress").innerHTML = state.data.enrDetail.map((e) => {
      const canStart = isAdmin && e.stage === "enrolled";
      const canRecord = isAdmin && e.stage === "learning";
      const canAdvance = isAdmin && e.stage === "learning";
      const att = (e.attendance || []).map((a) =>
        `<div class="score"><span>${esc(a.lesson_date)} ${a.present ? "出勤" : "缺课"}</span><span>${a.hours} 课时</span></div>`
      ).join("") || emptyMini();
      const prac = (e.practicals || []).map((p) =>
        `<div class="score"><span>${esc(p.title)}</span><span>${p.passed ? "合格" : "不合格"}</span></div>`
      ).join("") || emptyMini();
      return `<div class="row">
        <h4>${esc(e.student.name)} · ${esc(e.course.name)} ${stageTag(e.stage)}</h4>
        ${gateHtml(e.gate)}
        <div class="grid3c">
          <div class="meta"><b>考勤</b>${att}</div>
          <div class="meta"><b>实操</b>${prac}</div>
          <div class="meta"><b>考核</b> ${e.assessments.length} 单 / 连续合格 ${e.consecutive_passes}</div>
        </div>
        <div class="rowactions">
          ${canStart ? `<button class="btn small teal" data-act="start" data-id="${e.id}">进入学习</button>` : ""}
          ${canRecord ? `
            <button class="btn small green" data-act="attpresent" data-id="${e.id}">记一次出勤(+8课时)</button>
            <button class="btn small warn" data-act="attabsent" data-id="${e.id}">记一次缺课</button>
            <button class="btn small green" data-act="practice" data-id="${e.id}">记一次合格实操</button>
            <button class="btn small grey" data-act="practicefail" data-id="${e.id}">记一次不合格实操</button>
          ` : ""}
          ${canAdvance ? `<button class="btn small" data-act="advance" data-id="${e.id}">进入考核阶段</button>` : ""}
        </div>
      </div>`;
    }).join("") || empty();
  }

  // --------------------------------------------------------------- 渲染：考核
  function renderAssessments() {
    const isExaminer = meIs("examiner");
    const isAdmin = meIs("admin");
    const items = state.data.asmDetail.slice().sort((a, b) => b.created_at.localeCompare(a.created_at));
    $("#assessmentList").innerHTML = items.map((a) => {
      const enr = state.data.enrDetail.find((e) => e.id === a.enrollment_id);
      const who = enr ? `${enr.student.name} · ${enr.course.name}` : a.enrollment_id;
      const locked = a.result_locked;
      const scores = (a.scores || []).map((sc) =>
        `<div class="score"><span>${esc(sc.examiner_name)}</span><span>${sc.score} 分</span></div>`
      ).join("") || emptyMini();
      const vals = (a.scores || []).map((s) => s.score);
      const gap = vals.length ? Math.max(...vals) - Math.min(...vals) : 0;
      const mine = (a.scores || []).some((s) => s.examiner_id === state.me.id);
      let actions = "";
      if (!locked && a.status !== "in_review") {
        if (isExaminer && !mine)
          actions += `<button class="btn small teal" data-act="score" data-id="${a.id}">我来评分（${state.me.name}）</button>`;
        if (isAdmin)
          actions += `<button class="btn small" data-act="finalize" data-id="${a.id}">管理员评定收口</button>`;
      }
      if (isAdmin && !locked && a.status === "open" && !a.children.length)
        actions += `<button class="btn small warn" data-act="review" data-id="${a.id}">手动开复评</button>`;
      return `<div class="row ${a.kind === "review" ? "key" : ""}">
        <h4>${esc(who)} ${tag(kindLabel(a.kind), a.kind === "review" ? "violet" : "blue")}
          <span>第 ${a.round} 轮 ${asmTag(a.status)}</span></h4>
        <div class="meta">合格线 ${a.pass_score}｜分差阈值 ${a.gap_threshold}｜需 ${a.examiner_required} 名考评员｜当前分差 ${gap}
          ${a.parent_id ? `<br>复评源考核：${esc(a.parent_id)}` : ""}</div>
        <div class="scores">${scores}</div>
        <div class="rowactions">${actions}
          ${locked ? `<span class="hint">已${a.status === "passed" ? "通过" : "评定不通过"}并锁定</span>` : ""}
        </div>
      </div>`;
    }).join("") || empty();
  }

  // --------------------------------------------------------------- 渲染：资格
  function renderQualifications() {
    const isAdmin = meIs("admin");
    const nowIso = new Date().toISOString().slice(0);
    $("#qualificationList").innerHTML = state.data.qualDetail
      .slice().sort((a, b) => b.issued_at.localeCompare(a.issued_at))
      .map((q) => {
        const expired = q.valid_until <= nowIso;
        const acts = isAdmin ? `
          ${q.status === "active" ? `<button class="btn small warn" data-act="suspend" data-id="${q.id}">暂停</button>` : ""}
          ${q.status === "suspended" ? `<button class="btn small green" data-act="restore" data-id="${q.id}">恢复</button>` : ""}
          ${["active", "suspended"].includes(q.status) ? `<button class="btn small danger" data-act="revoke" data-id="${q.id}">撤销</button>` : ""}
          <button class="btn small grey" data-act="devexpire" data-id="${q.id}">演练：置为到期</button>` : "";
        return `<div class="row ${q.status === "active" ? "key" : ""}">
          <h4>${esc(q.cert_no)} ${qualTag(q.status)} ${expired && q.status === "active" ? tag("接口判定已过期", "gray") : ""}</h4>
          <div class="meta">${esc(q.student.name)} · ${esc(q.skill.name)}${q.skill.key_process ? " ★关键工序" : ""}<br>
          发证 ${fmt(q.issued_at)}｜有效期至 ${fmt(q.valid_until)}｜连续合格 ${q.consecutive_passes}</div>
          <div class="rowactions">${acts}</div>
        </div>`;
      }).join("") || empty();
  }

  // --------------------------------------------------------------- 渲染：派工
  function renderAssignments() {
    const isAdmin = meIs("admin");
    $("#assignmentList").innerHTML = state.data.assignments.map((j) => {
      const st = nameOf(state.data.users, j.student_id);
      const ws = state.data.workstations.find((w) => w.id === j.workstation_id);
      const active = j.status === "active";
      return `<div class="row ${ws && ws.is_key ? "key" : ""}">
        <h4>${esc(st)} → ${esc(ws ? ws.name : j.workstation_id)}
          ${ws && ws.is_key ? tag("关键工序", "red") : tag("普通", "gray")}
          ${active ? tag("在岗", "green") : tag("已结束", "gray")}</h4>
        <div class="meta">开始 ${fmt(j.created_at)}${j.ended_at ? `｜结束 ${fmt(j.ended_at)}` : ""}
          ${j.end_reason ? `<br>结束原因：${esc(j.end_reason)}` : ""}</div>
        <div class="rowactions">
          ${active && isAdmin ? `<button class="btn small grey" data-act="endjob" data-id="${j.id}">结束派工</button>` : ""}
        </div>
      </div>`;
    }).join("") || empty();
  }

  // --------------------------------------------------------------- 渲染：审计
  function renderAudit() {
    $("#auditList").innerHTML = (state.data.audit || []).map((r) => `
      <div class="row audit-row">
        <div class="meta">
          <span class="actor">${r.actor_id ? esc(nameOf(state.data.users, r.actor_id)) : "系统"}</span>
          · ${esc(r.action)} · ${esc(r.entity)}${r.entity_id ? "/" + esc(r.entity_id) : ""}
          <span style="float:right">${fmt(r.created_at)}</span>
          ${r.detail ? `<div style="margin-top:4px;color:#55483a">${esc(r.detail)}</div>` : ""}
        </div>
      </div>`).join("") || empty();
  }

  const empty = () => `<div class="empty">暂无数据</div>`;
  const emptyMini = () => `<div class="empty" style="padding:2px">—</div>`;

  function renderAll() {
    fillSelects();
    renderBase(); renderEnrollments(); renderAssessments();
    renderQualifications(); renderAssignments(); renderAudit();
  }

  async function refresh() {
    try {
      await loadAll();
      renderAll();
    } catch (e) {
      toast(errText(e), "err");
    }
  }

  // --------------------------------------------------------------- 角色
  function meIs(role) { return state.me && state.me.role === role; }
  function applyRoleVisibility() {
    const admin = meIs("admin");
    const examiner = meIs("examiner");
    $$(".admin-only").forEach((b) => { b.style.display = admin ? "" : "none"; });
    // 考评员可以开考核单/评分；管理员也可开考核单（服务层允许），但评分只能考评员。
    const assessForm = $('form[data-create="assessment"]');
    assessForm.style.display = (admin || examiner) ? "" : "none";
    const certForm = $('form[data-create="certificate"]');
    certForm.style.display = admin ? "" : "none";
  }

  // --------------------------------------------------------------- 动作
  async function doAction(act, id) {
    try {
      switch (act) {
        case "start": await api("POST", `/api/enrollments/${id}/start`, {}, { idempotent: true }); break;
        case "advance": await api("POST", `/api/enrollments/${id}/advance`, {}, { idempotent: true }); break;
        case "attpresent": await api("POST", `/api/enrollments/${id}/attendance`,
          { lesson_date: today(), present: true, hours: 8 }, { idempotent: true }); break;
        case "attabsent": await api("POST", `/api/enrollments/${id}/attendance`,
          { lesson_date: today(), present: false, hours: 0 }, { idempotent: true }); break;
        case "practice": await api("POST", `/api/enrollments/${id}/practicals`,
          { title: `实操 ${new Date().toLocaleTimeString()}`, passed: true }, { idempotent: true }); break;
        case "practicefail": await api("POST", `/api/enrollments/${id}/practicals`,
          { title: `实操(不合格) ${new Date().toLocaleTimeString()}`, passed: false }, { idempotent: true }); break;
        case "score": {
          const v = prompt(`以「${state.me.name}」身份评分（0-100）`);
          if (v == null) return;
          const score = Number(v);
          await api("POST", `/api/assessments/${id}/scores`,
            { examiner_id: state.me.id, score }, { idempotent: true });
          break;
        }
        case "finalize": await api("POST", `/api/assessments/${id}/finalize`, {}, { idempotent: true }); break;
        case "review": await api("POST", `/api/assessments/${id}/review`, {}, { idempotent: true }); break;
        case "suspend": {
          const reason = prompt("暂停原因（可留空）") || "";
          await api("POST", `/api/qualifications/${id}/suspend`, { reason }, { idempotent: true }); break;
        }
        case "restore": await api("POST", `/api/qualifications/${id}/restore`, {}, { idempotent: true }); break;
        case "revoke": {
          const reason = prompt("撤销原因（可留空）") || "";
          await api("POST", `/api/qualifications/${id}/revoke`, { reason }, { idempotent: true }); break;
        }
        case "devexpire":
          if (!confirm("把该资格有效期改为过去（立即到期并停止派工）？")) return;
          await api("POST", `/api/dev/qualifications/${id}/expire`, {}, { idempotent: true }); break;
        case "endjob": {
          const reason = prompt("结束原因（可留空）") || "";
          await api("POST", `/api/assignments/${id}/end`, { reason }, { idempotent: true }); break;
        }
        default: return;
      }
      toast("操作成功", "ok");
      await refresh();
    } catch (e) {
      toast(`操作被拒绝/失败：\n${errText(e)}`, "err");
    }
  }

  function today() { return new Date().toISOString().slice(0, 10); }

  // --------------------------------------------------------------- 表单
  const FORM_PAYLOAD = {
    skill: (f) => ({
      name: f.name.value.trim(),
      key_process: f.key_process.checked,
      min_consecutive: Number(f.min_consecutive.value),
    }),
    course: (f) => ({
      name: f.name.value.trim(), skill_id: f.skill_id.value,
      required_hours: Number(f.required_hours.value),
      required_practice: Number(f.required_practice.value),
      allowed_absences: Number(f.allowed_absences.value),
    }),
    workstation: (f) => ({
      name: f.name.value.trim(), is_key: f.is_key.checked,
      skill_id: f.skill_id.value || null,
    }),
    user: (f) => ({ name: f.name.value.trim(), role: f.role.value }),
    enrollment: (f) => ({ student_id: f.student_id.value, course_id: f.course_id.value }),
    assessment: (f) => ({
      enrollment_id: f.enrollment_id.value, kind: f.kind.value,
      pass_score: Number(f.pass_score.value),
      gap_threshold: Number(f.gap_threshold.value),
    }),
    certificate: (f) => ({
      enrollment_id: f.enrollment_id.value, valid_months: Number(f.valid_months.value),
    }),
    assignment: (f) => ({ student_id: f.student_id.value, workstation_id: f.workstation_id.value }),
  };
  const FORM_PATH = {
    skill: "/api/skills", course: "/api/courses", workstation: "/api/workstations",
    user: "/api/users", enrollment: "/api/enrollments", assessment: "/api/assessments",
    certificate: "/api/certificates", assignment: "/api/assignments",
  };
  const FORM_NAME = {
    skill: "技能", course: "课程", workstation: "工位", user: "人员",
    enrollment: "报名", assessment: "考核单", certificate: "资格证", assignment: "派工",
  };

  function bindForms() {
    $$("form[data-create]").forEach((form) => {
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const kind = form.dataset.create;
        const btn = form.querySelector("button[type=submit], button");
        let payload;
        try {
          payload = FORM_PAYLOAD[kind](form);
        } catch (e) { toast(e.message, "err"); return; }
        btn.disabled = true;
        try {
          const created = await api("POST", FORM_PATH[kind], payload, { idempotent: true });
          toast(`${FORM_NAME[kind]}已建立${created.cert_no ? "：" + created.cert_no : ""}`, "ok");
          form.reset();
          await refresh();
        } catch (e) {
          toast(`提交被拒绝：\n${errText(e)}`, "err");
        } finally {
          btn.disabled = false;
        }
      });
    });
  }

  // --------------------------------------------------------------- 初始化
  function bindTabs() {
    $$("#tabs .tab").forEach((t) => t.addEventListener("click", () => {
      $$("#tabs .tab").forEach((x) => x.classList.toggle("active", x === t));
      const name = t.dataset.tab;
      $$(".pane").forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== name));
    }));
  }

  function bindDelegations() {
    document.addEventListener("click", (ev) => {
      const b = ev.target.closest("button[data-act]");
      if (b) doAction(b.dataset.act, b.dataset.id);
    });
    $("#refreshBtn").addEventListener("click", refresh);
    $("#makeExpiredBtn").addEventListener("click", async () => {
      const active = state.data.qualifications.find((q) => q.status === "active");
      if (!active) return toast("当前没有 active 资格可演练", "info");
      try {
        await api("POST", `/api/dev/qualifications/${active.id}/expire`, {}, { idempotent: true });
        toast("已将一张资格置为到期，并立即停止其派工", "ok");
        await refresh();
      } catch (e) { toast(errText(e), "err"); }
    });
  }

  function bindIdentity() {
    const sel = $("#userSelect");
    sel.addEventListener("change", () => {
      state.me = state.data.users.find((u) => u.id === sel.value) || null;
      applyRoleVisibility();
      renderAll();
    });
  }

  function populateIdentity() {
    const sel = $("#userSelect");
    const roleLabel = { admin: "管理员", examiner: "考评员", student: "学员" };
    sel.innerHTML = state.data.users
      .slice().sort((a, b) => a.role.localeCompare(b.role))
      .map((u) => `<option value="${u.id}">【${roleLabel[u.role]}】${u.name}</option>`).join("");
    const admin = state.data.users.find((u) => u.role === "admin");
    sel.value = admin.id;
    state.me = admin;
  }

  async function init() {
    bindTabs(); bindDelegations(); bindForms(); bindIdentity();
    try {
      const users = await api("GET", "/api/users");
      state.data.users = users;
      populateIdentity();
      applyRoleVisibility();
      await loadAll();
      renderAll();
    } catch (e) {
      toast("初始化失败：" + errText(e), "err");
    }
  }

  init();
})();
