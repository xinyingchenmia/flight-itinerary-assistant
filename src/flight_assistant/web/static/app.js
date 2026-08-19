const $ = (sel) => document.querySelector(sel);
const show = (sel) => $(sel).classList.remove("hidden");
const hide = (sel) => $(sel).classList.add("hidden");

// 点"待确认"图章直接跳到规划助手那栏、预填这条问题，不用自己去菜单里找。
// 规划助手在导入结果后就已经自动开始跑了，这里只是把输入框亮出来。
document.addEventListener("click", (e) => {
  const row = e.target.closest(".pending-clickable");
  if (!row) return;
  const question = row.dataset.question || "";
  show("#step-clarify");
  show("#clarify-input-row");
  const input = $("#clarify-custom");
  input.value = `关于「${question}」：`;
  $("#step-clarify").scrollIntoView({ behavior: "smooth", block: "start" });
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
});

let sessionId = null;

const SEVERITY_LABEL = { blocker: "无法成行", major: "重要", minor: "留意" };
const VERDICT_ICON = { good: "☺", poor: "☹", unknown: "?" };

// ---- 步骤 1：自然语言 → 查询解析 agent ----
let parsedQuery = null;

const SORT_LABEL = { price: "价格", duration: "总时长", stops: "中转次数", layover: "中转等待时长" };
const GROUND_LABEL = { taxi_ok: "可以打车", public_only: "只能公共交通", unknown: "不确定" };
const LAYOVER_LABEL = { shorter: "越短越好", explore: "愿意长中转", no_preference: "无所谓", unknown: "不确定" };
const FIELD_LABEL = { origin: "出发机场", dest: "到达机场", date: "日期" };

$("#query-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("#query-text").value.trim();
  if (!text) return;

  $("#parse-btn").disabled = true;
  $("#parse-btn").textContent = "理解中…";
  try {
    const res = await fetch("/api/parse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, budget: Number($("#query-budget").value) || 0.5 }),
    });
    if (!res.ok) {
      alert("解析失败: " + (await res.text()));
      return;
    }
    parsedQuery = await res.json();
    renderConfirm(parsedQuery);
    show("#step-confirm");
    $("#step-confirm").scrollIntoView({ behavior: "smooth" });
  } finally {
    $("#parse-btn").disabled = false;
    $("#parse-btn").textContent = "开始";
  }
});

function renderConfirm(p) {
  // summary 是 agent 自己写的大白话确认句，已经涵盖了国籍/护照/偏好这些
  // 内容——不在这里用"字段名: 值"的格式重复一遍，那种格式对用户不友好。
  // 这里只再补一行最关键、agent 的 summary 未必会点出来的路线信息。
  $("#confirm-summary").textContent = p.summary || "";

  const route = `${p.origin} → ${p.dest}　${p.date}　按${SORT_LABEL[p.sort_pref] || p.sort_pref}排序`;
  $("#confirm-details").textContent = route;

  // 只有 origin/dest/date 缺了才真正拦得住——没有这三样连携程搜索链接
  // 都拼不出来。国籍/护照/托运件数这些不是取数的前提，就算 agent 没
  // 完全守住 prompt 里的规则把它们也塞进了 missing，这里也不该拦，
  // 那些缺口该在澄清对话那一步问，不是在这里卡住你。
  const HARD_REQUIRED = ["origin", "dest", "date"];
  const hardMissing = (p.missing || []).filter((m) => HARD_REQUIRED.includes(m));

  const missingBox = $("#confirm-missing");
  if (hardMissing.length) {
    missingBox.textContent =
      "没听清楚：" + hardMissing.map((m) => FIELD_LABEL[m] || m).join("、") + "——回去补充一下再继续。";
    show("#confirm-missing");
    $("#confirm-go").disabled = true;
  } else {
    hide("#confirm-missing");
    $("#confirm-go").disabled = false;
  }
}

$("#confirm-back").onclick = () => {
  hide("#step-confirm");
  $("#step-query").scrollIntoView({ behavior: "smooth" });
};

$("#confirm-go").onclick = async () => {
  const p = parsedQuery;
  const body = {
    origin: p.origin,
    dest: p.dest,
    date: p.date,
    sort: p.sort_pref,
    max_stops: p.max_stops,
    budget: p.budget,
    soft_prefs: p.soft_preferences || [],
    nationality: p.nationality,
    passport_expiry: p.passport_expiry,
    destination_after_arrival: p.destination_after_arrival,
    ground_transport_ok: p.ground_transport_ok,
    checked_bags: p.checked_bags,
    layover_preference: p.layover_preference,
  };

  const res = await fetch("/api/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    alert("发起查询失败: " + (await res.text()));
    return;
  }
  const data = await res.json();
  sessionId = data.session_id;

  $("#ctrip-link").href = data.ctrip_url;
  $("#bookmarklet-link").href = data.capture_bookmarklet;
  $("#capture-script-box").value = data.capture_script;
  show("#step-capture");
  $("#step-capture").scrollIntoView({ behavior: "smooth" });
  startCapturePolling();
};

// ---- 步骤 2：复制脚本（DevTools 兜底方式） ----
$("#copy-script").onclick = async () => {
  await navigator.clipboard.writeText($("#capture-script-box").value);
  $("#copy-script").textContent = "已复制 ✓";
  setTimeout(() => ($("#copy-script").textContent = "复制脚本"), 1500);
};

// ---- 自动轮询：文件一出现在 ~/Downloads 就自动导入，不用手动点 ----
let capturePolling = false;
let importing = false;

async function startCapturePolling() {
  capturePolling = true;
  while (capturePolling) {
    await new Promise((r) => setTimeout(r, 2500));
    if (!capturePolling || importing) continue;
    try {
      const res = await fetch(`/api/session/${sessionId}/capture-status`);
      const data = await res.json();
      if (data.ready) {
        $("#import-status").textContent = "检测到抓包文件，自动导入中…";
        $("#import-status").className = "ready";
        await doImport();
        return;
      }
    } catch (e) {
      // 轮询失败不打断，下一轮再试
    }
  }
}

async function doImport() {
  if (importing) return;
  importing = true;
  capturePolling = false;
  $("#import-status").textContent = "正在导入并跑风险审查（可能要 20-60 秒）…";
  $("#import-status").className = "";
  $("#import-btn").disabled = true;
  try {
    const res = await fetch(`/api/session/${sessionId}/import`, { method: "POST" });
    if (!res.ok) {
      $("#import-status").textContent = "失败: " + (await res.text());
      $("#import-btn").disabled = false;
      importing = false;
      capturePolling = true; // 失败了继续轮询，可能是文件还没写完
      return;
    }
    const data = await res.json();
    $("#import-status").textContent = "";
    renderResults(data);
    show("#step-results");
    $("#step-results").scrollIntoView({ behavior: "smooth" });

    // 不再按"有没有需要确认的风险"决定要不要进入这一步——行程规划助手
    // 自己判断有没有值得建议的东西，哪怕风险审查没标任何需要确认的项，
    // 它也可能想到"中转很久，要不要查查机场怎么打发时间"这类建议。
    startClarify();
  } finally {
    $("#import-btn").disabled = false;
  }
}

$("#import-btn").onclick = doImport;

// ---- 渲染候选卡片（复用同一份逻辑给结果/最终结果两处用） ----
function fmtTime(iso) {
  // 后端给的是不带时区的本地时间 ISO 字符串（航班当地时间），直接摘
  // 月/日 时:分，不做时区换算——时区换算会把"当地时间"变成误导信息。
  const m = iso.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if (!m) return iso;
  return `${Number(m[2])}/${Number(m[3])} ${m[4]}:${m[5]}`;
}

// 图章文案 + 样式类，风险审查结论按"海关审查"的感觉呈现——
// HOLD(红/扣留) = blocker，NOTE(黄/留意) = major/minor，
// PENDING(灰/虚线) = needs_user_input（还没审完，等你补充信息），
// CLEAR(绿/放行) = 确认没问题的项。
const STAMP_TEXT = { blocker: "扣留 HOLD", major: "留意 NOTE", minor: "留意 NOTE" };
const STAMP_CLASS = { blocker: "stamp-hold", major: "stamp-note", minor: "stamp-note" };
// 每个章给一点不同的倾斜角度，看起来不是机器整齐盖的
const STAMP_ROTATIONS = [-7, 5, -4, 8, -9, 3, -5, 6];

function stampHtml(cls, text, idx) {
  const rot = STAMP_ROTATIONS[idx % STAMP_ROTATIONS.length];
  return `<span class="stamp ${cls}" style="--rot:${rot}deg; animation-delay:${idx * 0.08}s">${text}</span>`;
}

function renderCandidateCard(c) {
  const div = document.createElement("div");
  div.className = "candidate";

  const conn =
    c.connections.length === 0
      ? "直飞"
      : c.connections.map((x) => `${x.airport} 停 ${Math.floor(x.gap_min / 60)}h${String(x.gap_min % 60).padStart(2, "0")}m`).join(" / ");

  const hasBlocker = c.issues.some((r) => r.severity === "blocker");
  const hasIssue = c.issues.length > 0 || c.unmet_preferences.some((n) => n.verdict === "poor");

  // 整张候选的"综合裁定"大章，贴在卡片角上
  const verdict = hasBlocker
    ? { cls: "verdict-hold", text: "不建议通行" }
    : hasIssue
    ? { cls: "verdict-note", text: "需要留意" }
    : { cls: "verdict-clear", text: "一路畅通" };

  let stampIdx = 0;
  let html = `
    <div class="verdict-stamp ${verdict.cls}">${verdict.text}</div>
    <div class="head">
      <span class="price">#${c.rank} ¥${c.price}</span>
      <span class="route">${c.route.join(" → ")}　${Math.floor(c.duration_min / 60)}h${String(c.duration_min % 60).padStart(2, "0")}m　${conn}</span>
    </div>
    <div class="flight-legs">
  `;
  for (const f of c.flights) {
    html += `<div class="leg">${f.carrier}${f.flight_no.slice(f.carrier.length)}　${f.dep_airport} ${fmtTime(f.dep_time)} → ${f.arr_airport} ${fmtTime(f.arr_time)}</div>`;
  }
  html += `</div>`;
  html += `<div class="sub-line">${c.ticket_count > 1 ? "⚠ 分 " + c.ticket_count + " 张票　" : ""}${c.platforms.join("/")}</div>`;

  if (c.issues.length) {
    html += `<div class="bucket-label">需要确认的问题</div>`;
    for (const r of c.issues) {
      if (r.needs_user_input) {
        const stamp = stampHtml("stamp-pending", "待确认 PENDING", stampIdx++);
        html += `<div class="finding-row pending-clickable" data-question="${escapeHtml(r.evidence)}">${stamp}<span class="finding-text">${escapeHtml(r.evidence)}<span class="click-hint">点击直接补充信息 →</span></span></div>`;
      } else {
        const stamp = stampHtml(STAMP_CLASS[r.severity], STAMP_TEXT[r.severity], stampIdx++);
        html += `<div class="finding-row">${stamp}<span class="finding-text">${escapeHtml(r.evidence)}</span></div>`;
      }
    }
  }

  if (c.confirmed_ok.length) {
    html += `<div class="bucket-label">没问题的地方</div>`;
    for (const item of c.confirmed_ok) {
      const stamp = stampHtml("stamp-clear", "放行 CLEAR", stampIdx++);
      if (item._kind === "assurance") {
        html += `<div class="finding-row">${stamp}<span class="finding-text">${escapeHtml(item.statement)}<span class="evidence">依据：${escapeHtml(item.evidence)}</span></span></div>`;
      } else {
        html += `<div class="finding-row">${stamp}<span class="finding-text">关于「${escapeHtml(item.preference)}」：${escapeHtml(item.statement)}</span></div>`;
      }
    }
  }

  if (c.unmet_preferences.length) {
    html += `<div class="bucket-label">没满足的偏好</div>`;
    for (const n of c.unmet_preferences) {
      if (n.verdict === "poor") {
        const stamp = stampHtml("stamp-note", "留意 NOTE", stampIdx++);
        html += `<div class="finding-row">${stamp}<span class="finding-text">关于「${escapeHtml(n.preference)}」：${escapeHtml(n.statement)}</span></div>`;
      } else {
        const stamp = stampHtml("stamp-pending", "待确认 PENDING", stampIdx++);
        const q = `关于「${n.preference}」：${n.statement}`;
        html += `<div class="finding-row pending-clickable" data-question="${escapeHtml(q)}">${stamp}<span class="finding-text">关于「${escapeHtml(n.preference)}」：${escapeHtml(n.statement)}<span class="click-hint">点击直接补充信息 →</span></span></div>`;
      }
    }
  }

  if (!c.issues.length && !c.confirmed_ok.length && !c.unmet_preferences.length) {
    html += `<div class="finding">（未发现需要提示的问题）</div>`;
  }

  div.innerHTML = html;
  return div;
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s ?? "";
  return d.innerHTML;
}

function renderResults(data) {
  $("#results-meta").textContent =
    `共抓到 ${data.pool_size} 个候选，实际送审 ${data.pool_examined} 个（不是全部 ${data.pool_size} 个都跑了 AI），` +
    `耗时 ${data.elapsed_s.toFixed(0)}s，成本 $${data.cost_usd.toFixed(4)}，` +
    `AI 分析已花费 $${data.budget_spent.toFixed(4)} / $${data.budget_cap.toFixed(2)}` +
    (data.pending_clarifications ? `，有 ${data.pending_clarifications} 条待澄清` : "");

  const skippedBox = $("#skipped-note");
  skippedBox.innerHTML = "";
  if (data.selection_note) {
    const p = document.createElement("div");
    p.className = "skipped-list";
    p.textContent = "挑选依据：" + data.selection_note;
    skippedBox.appendChild(p);
  }
  if (data.ignored_wrong_date && data.ignored_wrong_date.length) {
    const p = document.createElement("div");
    p.className = "wrong-date-note";
    p.textContent =
      "⚠ 抓到的数据里有一部分是 " + data.ignored_wrong_date.join("、") +
      " 这些日期的，已经忽略，只用了你要查的那天。";
    skippedBox.appendChild(p);
  }

  const box = $("#candidates");
  box.innerHTML = "";
  for (const c of data.candidates) box.appendChild(renderCandidateCard(c));
}

// ---- 步骤 4：澄清对话（长轮询） ----
const PRIORITY_LABEL = { high: "建议先看这个", normal: "" };
let shownTipLabels = new Set();

async function startClarify() {
  show("#step-clarify");
  $("#clarify-intro").textContent = "行程规划助手正在看有什么值得帮你深挖的……";
  shownTipLabels = new Set();
  await fetch(`/api/session/${sessionId}/clarify/start`, { method: "POST" });
  pollClarify();
}

function renderTips(tips) {
  const box = $("#clarify-tips");
  for (const t of tips) {
    if (shownTipLabels.has(t.label + t.statement)) continue;
    shownTipLabels.add(t.label + t.statement);
    const div = document.createElement("div");
    div.className = "tip-card";
    div.innerHTML = `<div class="tip-label">${escapeHtml(t.label)}</div>
      <div class="tip-statement">${escapeHtml(t.statement)}</div>
      <div class="tip-evidence">依据：${escapeHtml(t.evidence)}</div>`;
    box.appendChild(div);
  }
}

function renderMenu(items) {
  const box = $("#clarify-menu");
  box.innerHTML = "";
  if (!items.length) return;
  for (const it of items) {
    const row = document.createElement("label");
    row.className = "menu-item";
    row.innerHTML = `<input type="checkbox" value="${it.id}" />
      <span class="menu-label">${escapeHtml(it.label)}${it.priority === "high" ? '<span class="tag">建议先看这个</span>' : ""}</span>
      <span class="menu-based-on">依据：${escapeHtml(it.based_on)}</span>`;
    box.appendChild(row);
  }
}

async function pollClarify() {
  while (true) {
    const res = await fetch(`/api/session/${sessionId}/clarify/poll`);
    const data = await res.json();
    renderTips(data.tips || []);
    if (data.warning) {
      $("#clarify-warning").textContent = "⚠ " + data.warning;
      show("#clarify-warning");
    }
    if (data.done) {
      hide("#clarify-input-row");
      $("#clarify-menu").innerHTML = "";
      $("#clarify-intro").textContent = data.error
        ? "（行程规划助手出错: " + data.error + "）"
        : "行程规划助手完成了，没有更多建议了。";
      finalize();
      return;
    }
    if (data.menu && data.menu.length) {
      $("#clarify-intro").textContent = "要不要我帮你查查这些？勾选想深挖的，或者留空直接跳过。";
      renderMenu(data.menu);
      show("#clarify-input-row");
      return; // 等用户提交，提交后会重新调用 pollClarify()
    }
    // 超时轮询没有新菜单，继续等
  }
}

$("#clarify-send").onclick = () => sendSelection(false);
$("#clarify-skip").onclick = () => sendSelection(true);

async function sendSelection(skip) {
  const selectedIds = skip
    ? []
    : Array.from($("#clarify-menu").querySelectorAll("input:checked")).map((el) => el.value);
  const custom = skip ? "" : $("#clarify-custom").value.trim();
  $("#clarify-custom").value = "";
  $("#clarify-menu").innerHTML = "";
  hide("#clarify-input-row");
  await fetch(`/api/session/${sessionId}/clarify/answer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ selected_ids: selectedIds, custom }),
  });
  pollClarify();
}

// ---- 步骤 5：最终状态（原地更新步骤 3 的卡片，不重复展示一遍） ----
async function finalize() {
  const res = await fetch(`/api/session/${sessionId}/finalize`, { method: "POST" });
  const data = await res.json();
  show("#step-final");
  $("#step-final").scrollIntoView({ behavior: "smooth" });

  let meta = `AI 分析已花费 $${data.budget_spent.toFixed(4)} / $${data.budget_cap.toFixed(2)}，采纳更新 ${data.updates_applied} 条`;
  if (data.warning) meta += `　⚠ ${data.warning}`;
  $("#final-meta").textContent = meta;

  const box = $("#candidates");
  box.innerHTML = "";
  for (const c of data.candidates) box.appendChild(renderCandidateCard(c));
}

// ---- 演示模式：访问 ?demo=1 用假数据直接看视觉效果，不调用任何 API、不花钱 ----
if (new URLSearchParams(location.search).get("demo") === "1") {
  const FAKE_CANDIDATES = [
    {
      rank: 1, price: "6808", route: ["PVG", "HKG", "ORD"], duration_min: 1175, stop_count: 1,
      connections: [{ airport: "HKG", gap_min: 120 }], ticket_count: 1, platforms: ["ctrip"],
      flights: [
        { carrier: "CX", flight_no: "CX303", dep_airport: "PVG", arr_airport: "HKG", dep_time: "2026-09-26T07:20:00", arr_time: "2026-09-26T09:50:00" },
        { carrier: "CX", flight_no: "CX806", dep_airport: "HKG", arr_airport: "ORD", dep_time: "2026-09-26T11:50:00", arr_time: "2026-09-26T13:55:00" },
      ],
      issues: [
        { severity: "blocker", evidence: "香港中转仅 120 分钟，且需更换航站楼，官方最短转机时间 90 分钟起，加上安检排队风险很大", needs_user_input: false },
        { severity: "minor", evidence: "护照有效期未提供，需要确认是否满足六个月有效期要求", needs_user_input: true },
      ],
      confirmed_ok: [{ _kind: "assurance", statement: "行李可直挂到芝加哥，无需在香港重新托运", evidence: "国泰航空联程票行李直挂政策" }],
      unmet_preferences: [{ verdict: "poor", preference: "尽量转机次数少", statement: "这条有一次转机，不是直飞" }],
    },
    {
      rank: 2, price: "7200", route: ["PVG", "ORD"], duration_min: 780, stop_count: 0,
      connections: [], ticket_count: 1, platforms: ["ctrip", "feizhu"],
      flights: [{ carrier: "UA", flight_no: "UA857", dep_airport: "PVG", arr_airport: "ORD", dep_time: "2026-09-26T13:00:00", arr_time: "2026-09-26T15:00:00" }],
      issues: [],
      confirmed_ok: [
        { _kind: "assurance", statement: "直飞航班，无中转风险", evidence: "行程仅一段航班" },
        { _kind: "preference", preference: "价格低", statement: "这是三个候选里最便宜的一个", verdict: "good" },
      ],
      unmet_preferences: [],
    },
    {
      rank: 3, price: "6100", route: ["PVG", "NRT", "ORD"], duration_min: 1320, stop_count: 1,
      connections: [{ airport: "NRT", gap_min: 295 }], ticket_count: 1, platforms: ["ctrip"],
      flights: [
        { carrier: "NH", flight_no: "NH922", dep_airport: "PVG", arr_airport: "NRT", dep_time: "2026-09-26T09:00:00", arr_time: "2026-09-26T12:30:00" },
        { carrier: "NH", flight_no: "NH110", dep_airport: "NRT", arr_airport: "ORD", dep_time: "2026-09-26T17:25:00", arr_time: "2026-09-26T15:50:00" },
      ],
      issues: [{ severity: "major", evidence: "东京中转近 5 小时，明显长于常规转机时间，但不构成风险", needs_user_input: false }],
      confirmed_ok: [{ _kind: "assurance", statement: "全日空全程承运，行李直挂无需重新托运", evidence: "全日空联程票政策" }],
      unmet_preferences: [],
    },
  ];

  const FAKE_MENU = [
    { id: "d1", label: "东京中转快 5 小时了，要不要我查查成田机场有什么好逛好吃的？", based_on: "候选三 NRT 中转 295 分钟", priority: "normal" },
    { id: "d2", label: "护照信息还没告诉我——这会决定香港中转要不要留意入境规则", based_on: "候选一有一条风险因此标了待确认", priority: "high" },
  ];
  // 故意不在一开始就摆结论——真实流程里 tips 是"选完之后 agent 去查了才有"
  // 的东西，演示模式也该一开始只有菜单，点了提交才"查到"结果，不然会让人
  // 误以为没等选择就已经查完了。
  const FAKE_TIP_AFTER_SUBMIT = {
    label: "落地芝加哥怎么去市区",
    statement: "打车到市区约 45 分钟、55 美元左右；地铁蓝线约 50 分钟、5 美元",
    evidence: "演示数据，非真实查询结果",
  };

  $("#results-meta").textContent = "演示模式 — 以下全部是假数据，不消耗真实查询";
  $("#candidates").innerHTML = "";
  for (const c of FAKE_CANDIDATES) $("#candidates").appendChild(renderCandidateCard(c));
  show("#step-results");

  $("#clarify-intro").textContent = "演示模式：要不要我帮你查查这些？勾选想深挖的，或者留空直接跳过。";
  renderMenu(FAKE_MENU);
  show("#clarify-menu");
  show("#clarify-input-row");
  show("#step-clarify");

  // 覆盖掉真实的提交按钮行为，模拟"选完之后 agent 查到了结果，候选卡片
  // 用新结论重新渲染"这个真实时序，不打真实 API。
  $("#clarify-send").onclick = () => {
    renderTips([FAKE_TIP_AFTER_SUBMIT]);
    $("#clarify-intro").textContent = "演示模式：完成了，没有更多建议了。";
    $("#clarify-custom").value = "";
    hide("#clarify-menu");
    hide("#clarify-input-row");

    // 模拟 finalize()：拿到答案后重新审查，候选一那条"护照有效期"从
    // 待确认变成放行——真实流程里这是后端用新 trip_context 重新跑一遍
    // 风险审查、把整张卡片重新渲染出来的结果，不是前端凭空改一个图章。
    const resolved = {
      ...FAKE_CANDIDATES[0],
      issues: FAKE_CANDIDATES[0].issues.filter((r) => !r.needs_user_input),
      confirmed_ok: [
        ...FAKE_CANDIDATES[0].confirmed_ok,
        { _kind: "assurance", statement: "护照有效期到 2031 年，满足六个月有效期要求", evidence: "你刚才确认的信息" },
      ],
    };
    const oldCard = document.querySelectorAll("#candidates .candidate")[0];
    oldCard.replaceWith(renderCandidateCard(resolved));
  };
  $("#clarify-skip").onclick = () => {
    $("#clarify-intro").textContent = "演示模式：跳过了，没有查任何东西。";
    hide("#clarify-menu");
    hide("#clarify-input-row");
  };

  $("#step-results").scrollIntoView({ behavior: "smooth" });
}
