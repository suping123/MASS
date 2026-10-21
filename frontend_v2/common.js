// 三端共用：token 管理、请求封装、轻提示
const TOKEN_KEY = "sca_token";
const USER_KEY = "sca_user";

const Auth = {
  token: () => localStorage.getItem(TOKEN_KEY) || "",
  user: () => {
    try { return JSON.parse(localStorage.getItem(USER_KEY) || "null"); }
    catch (e) { return null; }
  },
  save: (token, user) => {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(USER_KEY, JSON.stringify(user || {}));
  },
  clear: () => {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
  },
  require: (role) => {
    const u = Auth.user();
    if (!Auth.token() || !u) { location.href = "/login.html"; return null; }
    if (role && u.role !== role && u.role !== "admin") {
      alert("当前账号不能访问该平台，请用对应角色登录");
      location.href = "/login.html";
      return null;
    }
    return u;
  },
};

async function api(path, opt) {
  const o = Object.assign({ method: "GET", headers: {} }, opt || {});
  o.headers = Object.assign({ "Content-Type": "application/json" }, o.headers);
  const tk = Auth.token();
  if (tk) o.headers.Authorization = "Bearer " + tk;
  if (o.body && typeof o.body !== "string") o.body = JSON.stringify(o.body);
  const r = await fetch(path, o);
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (r.status === 401) {
    Auth.clear();
    location.href = "/login.html";
    throw new Error("未登录");
  }
  if (!r.ok) {
    const msg = (data && (data.detail || data.message)) || ("请求失败 " + r.status);
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

let toastTimer = null;
function toast(msg, ms) {
  let el = document.getElementById("__toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "__toast";
    el.className = "toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), ms || 2400);
}

function money(v) {
  if (v === null || v === undefined || v === "") return "-";
  return "¥" + Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

function esc(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// 品牌标识：内联 SVG（不引外部资源，离线也能显示）
const BRAND_SVG =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<circle cx="5" cy="6" r="2.1"/><circle cx="19" cy="18" r="2.1"/>' +
  '<path d="M7.1 6H13a3.4 3.4 0 0 1 0 6.8H9.4a3.2 3.2 0 0 0 0 6.4H17"/></svg>';

// 顶栏胶囊显示"平台"而不是"角色"：用户最先要确认的是"我现在在哪个平台"，
// 角色由账号决定，平台由角色决定，写平台名更不容易看错。
const ROLE_PLATFORM = {
  shipper: "货主平台", dispatcher: "调度平台",
  carrier: "承运平台", admin: "管理后台",
};

const ROLE_LABEL = {
  shipper: "货主", dispatcher: "调度员", carrier: "承运商", admin: "超级管理员",
};

// 通知铃铛：三端共用。平台改造前没有任何站内通知——调度改了时效、换了承运商，
// 货主只有刷新页面才可能发现，所以铃铛放在顶栏，未读数直接从接口取。
function bellHtml() {
  return '<div class="bell">' +
    '<button class="bell-btn" id="__bell" title="通知">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" ' +
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<path d="M6 9a6 6 0 1 1 12 0c0 4 1.5 5.5 2 6H4c.5-.5 2-2 2-6Z"/>' +
        '<path d="M9.5 19a2.5 2.5 0 0 0 5 0"/></svg>' +
      '<span class="bell-dot" id="__bellDot" hidden></span>' +
    '</button></div>';
}

async function bindBell() {
  const btn = document.getElementById("__bell");
  if (!btn) return;
  const dot = () => document.getElementById("__bellDot");
  const refresh = async () => {
    try {
      const u = await api("/api/notifications/unread");
      const d = dot();
      if (d) d.hidden = !(u && u.unread);
    } catch (e) { /* 未登录/接口未起时不打扰用户 */ }
  };
  await refresh();
  btn.onclick = async () => {
    try {
      const r = await api("/api/notifications?limit=30");
      const items = (r && r.items) || [];
      openDrawer("通知",
        items.length
          ? '<ul class="notif-list">' + items.map(n =>
              '<li class="' + (n.read ? "" : "unread") + '">' +
                '<div class="nf-top"><b>' + esc(n.title) + '</b>' +
                  '<span class="tag">' + esc(n.type_label) + '</span></div>' +
                (n.body ? '<div class="nf-body">' + esc(n.body) + '</div>' : "") +
                '<div class="nf-time">' + esc(n.created_at || "") + '</div></li>').join("") +
            '</ul>'
          : '<div class="empty">暂无通知</div>',
        items.length ? '<button class="small" id="__readAll">全部标为已读</button>' : "");
      const ra = document.getElementById("__readAll");
      if (ra) ra.onclick = async () => {
        try {
          await api("/api/notifications/read-all", { method: "POST" });
          const d = dot();
          if (d) d.hidden = true;
          toast("已全部标为已读");
          closeDrawer();
        } catch (e) { toast(e.message); }
      };
    } catch (e) { toast(e.message); }
  };
}

function mountTopbar(title, extra) {
  const u = Auth.user() || {};
  // 角色主题：CSS 用 body[data-role] 切换主色，避免三个端口在同一浏览器里认错
  document.body.dataset.role = u.role || "";
  const parts = String(title || "").split("·");
  const main = (parts[0] || "").trim();
  const sub = (parts.slice(1).join("·") || "").trim();
  const initial = (u.display_name || u.account || "?").trim().slice(0, 1).toUpperCase();
  const bar = document.createElement("div");
  bar.className = "topbar";
  bar.innerHTML =
    '<div class="brand">' +
      '<div class="brand-mark">' + BRAND_SVG + '</div>' +
      '<div class="brand-text">' +
        '<div class="brand-title">' + esc(main) + '</div>' +
        (sub ? '<div class="brand-sub">' + esc(sub) + '</div>' : '') +
      '</div>' +
    '</div>' +
    '<span class="spacer"></span>' +
    '<span class="role-pill" title="' + esc(ROLE_LABEL[u.role] || "") + '">' +
      esc(ROLE_PLATFORM[u.role] || ROLE_LABEL[u.role] || "未登录") + '</span>' +
    (extra || "") +
    bellHtml() +
    '<div class="who"><span class="who-name">' + esc(u.display_name || "") + '</span>' +
      '<span class="who-acc">' + esc(u.account || "") + '</span></div>' +
    '<div class="avatar">' + esc(initial) + '</div>' +
    '<button class="small ghost" id="__logout">退出</button>';
  document.body.prepend(bar);
  document.getElementById("__logout").onclick = () => {
    Auth.clear();
    location.href = "/login.html";
  };
  // 顶栏一挂好就把铃铛绑上，三端页面不用各自写一遍
  bindBell();
}

// 对话气泡
function pushMsg(box, role, text) {
  const d = document.createElement("div");
  d.className = "msg " + role;
  d.textContent = text;
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
  return d;
}

// 询问取消原因：返回 Promise<string|null>，null 表示用户放弃
function askReason(title, tip) {
  return new Promise((resolve) => {
    const mask = document.createElement("div");
    mask.className = "modal-mask show";
    mask.innerHTML =
      '<div class="modal"><h3>' + esc(title) + "</h3>" +
      '<div class="muted" style="margin-bottom:8px">' + esc(tip || "") + "</div>" +
      '<textarea id="__reason" rows="3" placeholder="请填写原因（会记入系统日志）"></textarea>' +
      '<div class="row"><button id="__no">不取消了</button>' +
      '<button class="primary" id="__yes">确认取消</button></div></div>';
    document.body.appendChild(mask);
    const ta = mask.querySelector("#__reason");
    ta.focus();
    const done = (v) => { mask.remove(); resolve(v); };
    mask.querySelector("#__no").onclick = () => done(null);
    mask.querySelector("#__yes").onclick = () => {
      const v = (ta.value || "").trim();
      if (v.length < 2) { toast("请填写原因（至少 2 个字）"); return; }
      done(v);
    };
  });
}

/* ================================================================
   详情抽屉 / 时间线 / 状态徽章
   ------------------------------------------------------------------
   列表只放"够扫一眼"的列；完整信息（金额构成、路线途经点、流转记录）
   放进右侧抽屉，避免表格被撑到需要横向滚动。
   ================================================================ */

let __drawerEl = null;

function closeDrawer() {
  if (!__drawerEl) return;
  const el = __drawerEl;
  __drawerEl = null;
  el.classList.remove("show");
  document.removeEventListener("keydown", __drawerEsc);
  setTimeout(() => el.remove(), 220);
}

function __drawerEsc(e) {
  if (e.key === "Escape") closeDrawer();
}

// 返回抽屉根节点，便于调用方继续挂事件
function openDrawer(title, bodyHtml, footHtml) {
  closeDrawer();
  const mask = document.createElement("div");
  mask.className = "drawer-mask";
  mask.innerHTML =
    '<aside class="drawer" role="dialog" aria-modal="true">' +
      '<div class="drawer-head"><h3>' + esc(title) + "</h3>" +
        '<button class="small ghost" data-drawer-close="1">关闭</button></div>' +
      '<div class="drawer-body">' + bodyHtml + "</div>" +
      (footHtml ? '<div class="drawer-foot">' + footHtml + "</div>" : "") +
    "</aside>";
  document.body.appendChild(mask);
  __drawerEl = mask;
  requestAnimationFrame(() => mask.classList.add("show"));
  document.addEventListener("keydown", __drawerEsc);
  mask.addEventListener("click", (e) => {
    if (e.target === mask) { closeDrawer(); return; }
    if (e.target.dataset && e.target.dataset.drawerClose) closeDrawer();
  });
  return mask;
}

const STATUS_TONE = {
  draft: "", submitted: "warn", planned: "warn", published: "ok",
  candidate: "warn", claimed: "ok", in_transit: "ok", exception: "danger",
  delivered: "ok", settled: "ok", cancelled: "", rejected: "danger", blocked: "danger",
};

function statusTag(status, label) {
  const t = STATUS_TONE[status];
  return '<span class="tag' + (t ? " " + t : "") + '">' + esc(label || status) + "</span>";
}

const EVENT_LABEL = {
  order_created: "订单创建", order_planned: "生成调度方案", order_published: "定价挂牌",
  carrier_candidate: "进入候选", order_claimed: "承运商接单", carrier_withdrew: "承运商退出",
  exception_reported: "上报运输异常", exception_resolved: "异常处置完成",
  order_cancelled: "订单取消", order_rolled_back: "状态回退",
  order_delivered: "货物送达", order_settled: "完成结算",
  // 本轮新增的三种处置结果。缺了它们时间线会掉回原始 type，
  // 货主看到 "order_deadline_extended" 这种内部标识等于什么都没说。
  order_rerouted: "改道绕行", order_deadline_extended: "延长送达时效",
  order_reassigned: "改派承运商", order_reassigned_to_pool: "退回挂牌池",
};

const ACTOR_LABEL = {
  shipper: "货主", dispatcher: "调度员", carrier: "承运商",
  admin: "管理员", system: "系统",
};

// 三种处置改的是"承诺"，时间线只写事件名等于没说清改了什么
function __shortTime(iso) {
  if (!iso) return "-";
  const s = String(iso);
  const m = s.match(/^\d{4}-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  return m ? (m[1] + "-" + m[2] + " " + m[3] + ":" + m[4]) : s;
}

function eventNote(e) {
  const p = e.payload || {};
  if (e.type === "order_deadline_extended" && p.after) {
    return "承诺 " + __shortTime(p.before) + " → " + __shortTime(p.after) +
      (p.delay_h ? "（延后 " + p.delay_h + " 小时）" : "");
  }
  if (e.type === "order_rerouted") {
    const o = p.old || {}, n = p.new || {};
    return "绕开 " + (p.avoid_city || "-") +
      (o.distance_km != null ? "，" + Math.round(o.distance_km) + " km → " : "，") +
      (n.distance_km != null ? Math.round(n.distance_km) + " km" : "-") +
      (p.detour_km != null ? "（" + (p.detour_km > 0 ? "+" : "") + p.detour_km + " km）" : "");
  }
  if (e.type === "order_reassigned" || e.type === "order_reassigned_to_pool") {
    return (p.reason_label || "改派") +
      (p.mode === "pool" ? "，已退回挂牌池重新找人" : "，已指定新承运商");
  }
  return "";
}

function eventTimeline(events) {
  const rows = events || [];
  if (!rows.length) return '<div class="empty">暂无流转记录</div>';
  return '<ol class="tl">' + rows.map((e) => {
    const name = EVENT_LABEL[e.type] || e.type;
    const who = e.actor ? ACTOR_LABEL[e.actor] || e.actor : "";
    const note = eventNote(e);
    return "<li><span class=\"tl-dot\"></span><div class=\"tl-main\"><b>" + esc(name) + "</b>" +
      (who ? '<span class="tl-actor">' + esc(who) + "</span>" : "") +
      (note ? '<div class="tl-note">' + esc(note) + "</div>" : "") +
      (e.at ? '<div class="tl-time">' + esc(e.at) + "</div>" : "") +
      "</div></li>";
  }).join("") + "</ol>";
}

// 区块：标题 + 内容，抽屉里统一用它分段
function detailSection(title, inner) {
  return '<section class="detail-section"><h4>' + esc(title) + "</h4>" + inner + "</section>";
}

// 键值网格。pairs: [[标签, 值HTML], ...]
function kvGrid(pairs) {
  return '<div class="kv">' + pairs.map(function (p) {
    return '<div class="k">' + esc(p[0]) + "</div><div>" + (p[1] == null ? "-" : p[1]) + "</div>";
  }).join("") + "</div>";
}

// 金额构成：一行一条，label 在左、金额在右，末行可强调
function amountList(items) {
  return '<div class="amount-list">' + items.map(function (it) {
    return '<div class="amount-row' + (it.strong ? " strong" : "") +
      (it.tone ? " " + it.tone : "") + '"><span>' + esc(it.label) + "</span><b>" +
      (it.value == null ? "-" : it.value) + "</b>" +
      (it.note ? '<em>' + esc(it.note) + "</em>" : "") + "</div>";
  }).join("") + "</div>";
}

/* ================================================================
   竞价：状态徽章 / 比价表 / 还价时间线（三端共用）
   ------------------------------------------------------------------
   三端看到的信息量不同（调度看全部报价，承运商只看自己那份），
   但"怎么把一条报价说清楚"是同一件事，所以渲染放在这里。
   ================================================================ */

const TENDER_TONE = {
  open: "warn", negotiating: "warn", awarded: "ok",
  failed: "danger", cancelled: "",
};

const TENDER_LABEL = {
  open: "收标中", negotiating: "协商中", awarded: "已定标",
  failed: "流标", cancelled: "已取消",
};

function tenderTag(status, label) {
  const t = TENDER_TONE[status];
  return '<span class="tag' + (t ? " " + t : "") + '">' +
    esc(label || TENDER_LABEL[status] || status) + "</span>";
}

// 倒计时只回答"还剩多久 / 超了多久"，不替人判断该不该裁决
function countdown(sec) {
  if (sec === null || sec === undefined) return "-";
  const s = Number(sec);
  const m = Math.round(Math.abs(s) / 60);
  if (s < 0) return "已过 " + m + " 分钟";
  if (m < 1) return "不到 1 分钟";
  return "还剩 " + m + " 分钟";
}

const VERDICT_LABEL = { winner: "中标", eligible: "合规未中标", rejected: "淘汰" };
// 落选不能一律写"淘汰"：超预算的那家不是被别人比下去的，是货主付不起。
// 结论列按原因码说话，比一个笼统的"淘汰"更能解释清楚。
const REASON_SHORT = {
  over_budget: "超可付上限", eta_miss: "时效不达标", capacity: "载重不足",
  temp: "无温控资质", hazmat: "无危品资质", deadhead: "空驶过远",
};

// 比价表：为什么是这家。
// 只给报价看不出"便宜的那家为什么没中"——所以综合分、结论、原因三列缺一不可。
// opts.winnerScore 用来给"合规但分低"的那家补一句人话解释。
function bidTable(bids, opts) {
  const rows = bids || [];
  if (!rows.length) return '<div class="empty">还没有承运商报价</div>';
  const o = opts || {};
  const winScore = (rows.find(b => b.verdict === "winner") || {}).score;
  const head = "<thead><tr><th>承运商</th><th>报价</th><th>承诺时效</th>" +
    "<th>空驶</th><th>评分</th><th>综合分</th><th>结论</th></tr></thead>";
  const body = rows.map(function (b) {
    const won = b.verdict === "winner";
    let why = b.reason || "";
    if (!why && b.verdict === "eligible" && winScore != null && b.score != null)
      why = "综合分 " + Number(b.score).toFixed(4) + " 低于中标家 " + Number(winScore).toFixed(4);
    if (!why && b.verdict === "eligible") why = "合规，但综合分不是最高";
    // 协商成交的单，报价列要能区分"我报了多少"和"最后成交多少"。
    // 承运商接受还价时会把报价改成成交价，这时两个数一样，不重复显示。
    const finalQ = (b.final_quote == null || Number(b.final_quote) === Number(b.quote)) ? "" :
      '<div class="muted" style="font-size:11px">成交 ' + money(b.final_quote) + "</div>";
    const label = won ? VERDICT_LABEL.winner
      : (b.verdict === "rejected" && REASON_SHORT[b.reason_code])
        || VERDICT_LABEL[b.verdict] || b.verdict || "待裁决";
    return '<tr class="' + (won ? "win" : "") + '">' +
      "<td>" + esc(b.carrier_org || ("机构 #" + b.carrier_org_id)) + "</td>" +
      "<td><b>" + money(b.quote) + "</b>" + finalQ + "</td>" +
      "<td>" + (b.eta_h == null ? "-" : Number(b.eta_h).toFixed(1) + " h") + "</td>" +
      "<td>" + (b.deadhead_km == null ? "-" : Number(b.deadhead_km).toFixed(0) + " km") + "</td>" +
      "<td>" + (b.rating == null ? "-" : Number(b.rating).toFixed(2)) + "</td>" +
      "<td>" + (b.score == null ? "-" : Number(b.score).toFixed(4)) + "</td>" +
      '<td class="verdict' + (won ? " won" : "") + '">' +
        esc(label) +
        (why ? '<em>' + esc(why) + "</em>" : "") + "</td></tr>";
  }).join("");
  return '<table class="cmp bid-table">' + head + "<tbody>" + body + "</tbody></table>" +
    (o.caption ? '<div class="muted" style="margin-top:6px">' + esc(o.caption) + "</div>" : "");
}

const NEG_ACTOR = { dispatch: "调度", carrier: "承运商" };
const NEG_ACTION = { counter: "出价", accept: "接受", reject: "拒绝", fail: "放弃" };

// 还价时间线：谁在第几轮出了多少、对方接不接受。
// 这是"价格不是单方定的"唯一能拿给人看的证据——只有结论没有回合，
// 看的人没法判断是真谈过还是走了个过场。
function negTimeline(neg) {
  const n = neg || {};
  const turns = n.turns || [];
  if (!turns.length) return '<div class="empty">还没有还价记录</div>';
  const head = '<div class="neg-head">第 ' + (n.rounds_used || n.round || 1) + " / " +
    (n.max_rounds || "-") + " 轮" +
    (n.outcome ? "　·　" + esc({ awarded: "已达成一致", failed: "谈不拢，交人工",
                                 expired: "超时未回应" }[n.outcome] || n.outcome) : "") + "</div>";
  return head + '<ol class="tl">' + turns.map(function (t) {
    const who = NEG_ACTOR[t.from] || t.from || "?";
    const act = NEG_ACTION[t.type] || t.type || "?";
    return "<li><span class=\"tl-dot\"></span><div class=\"tl-main\"><b>" + esc(act) +
      (t.amount != null ? " " + money(t.amount) : "") + "</b>" +
      '<span class="tl-actor">' + esc(who) + " · 第 " + (t.round || 1) + " 轮</span>" +
      (t.reason ? '<div class="tl-note">' + esc(t.reason) + "</div>" : "") +
      (t.at ? '<div class="tl-time">' + esc(t.at) + "</div>" : "") +
      "</div></li>";
  }).join("") + "</ol>";
}

// 裁决权重：价格/时效/评分/空驶各占多少。改过权重的人要能一眼看出
// "上次中标是因为价格，这次为什么换了家"。
const WEIGHT_LABEL = { price: "价格", eta: "时效", rating: "评分", deadhead: "空驶" };

function weightBar(weights) {
  const w = weights || {};
  const keys = ["price", "eta", "rating", "deadhead"].filter(k => w[k] != null);
  if (!keys.length) return "";
  return '<div class="wbar">' + keys.map(k =>
    '<span class="wseg" data-k="' + k + '" style="flex:' + Number(w[k]) + '">' +
    esc(WEIGHT_LABEL[k] || k) + " " + Math.round(Number(w[k]) * 100) + "%</span>").join("") +
    "</div>";
}
