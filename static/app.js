"use strict";
const $ = (s) => document.querySelector(s),
  esc = (x) =>
    String(x ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
const paths = {
  home: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  jobs: "M4 6h16 M4 12h16 M4 18h11",
  history: "M4 10a8 8 0 1 1 1 7 M4 4v6h6 M12 7v5l4 2",
  settings:
    "M3 6h8m5 0h5 M3 12h2m5 0h11 M3 18h10m5 0h3 M12 3v6 M7 9v6 M15 15v6",
};
function ico(k) {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[k === "mappings" ? "jobs" : k]}"/></svg>`;
}
const pair = (k, v) =>
  `<div class="pair"><span>${k}</span><span>${esc(v)}</span></div>`;
function notify(t) {
  $("#toast").textContent = t;
  $("#toast").classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("#toast").classList.remove("show"), 3200);
}
async function act(a, p = {}) {
  let res = await fetch("/api/" + a, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Relay-Request": "1",
      "X-CSRF-Token": csrf,
    },
    body: JSON.stringify(p),
  });
  let obj = await res.json();
  if (res.status === 401) {
    loginScreen("会话已过期，请重新登录。");
    throw Error("请重新登录");
  }
  if (!res.ok) throw Error(obj.error || "操作失败");
  await refresh(true);
  return obj;
}
const field = (t, name, placeholder = "", val = "") =>
  `<label>${t}<input required name="${name}" placeholder="${esc(placeholder)}" value="${esc(val)}" autocomplete="off"></label>`;
const nameField = (value = "") =>
  `<label>频道备注（可选）<input name="name" value="${esc(value)}" placeholder="例如：资料归档" autocomplete="off"></label>`;
function markedId(id) {
  // Valid channel IDs fit safely within JavaScript's exact integer range.
  const n = Number(id);
  return String(n > 0 ? -(1000000000000 + n) : n);
}
function channelLabel(id) {
  if (!id) return "未设置";
  const marked = markedId(id);
  const bare = -Number(marked) - 1000000000000;
  const name = s.channel_names?.[bare] || s.channel_names?.[marked];
  return name ? `${name}（${marked}）` : marked;
}
function modal(title, html, ok, fn) {
  $("#mt").textContent = title;
  $("#fields").innerHTML = html;
  $("#error").textContent = "";
  $("#submit").hidden = !fn;
  $("#submit").style.display = fn ? "" : "none";
  $("#submit").textContent = ok || "确认";
  callback = fn;
  if (!$("#dlg").open) $("#dlg").showModal();
}
function accountCards() {
  return `<section class="card">${head("账号登录")}<div class="authgrid">${[
    "tdl",
    "telethon",
  ]
    .map((k, i) => {
      const saved = s.accounts?.[k];
      const action = btn(
        i ? "telethonLogin" : "tdlLogin",
        saved ? "重新扫码登录" : "扫码登录",
      );
      return `<div class="authcard"><div class="cardhead"><span class="authmark">${i ? "TH" : "TD"}</span>${tag(saved ? "on" : "off", saved ? "会话已保存" : "未配置")}</div><h3>${i ? "Telethon 用户会话" : "TDL 转发账号"}</h3><p class="hint">${i ? "保存用户会话后自动连接，仅监听已启用的源频道。无用户会话时仍可用 Bot 监听；遇到禁转需要已授权的 TDL。" : "手动转发必需 · 容器已内置 TDL，无需在宿主机安装或执行登录命令。"}</p>${i ? `<p class="note">监听状态：${esc(s.listener_status)}</p>` : ""}${saved ? "<small>会话已保存不代表授权有效；失效后可重新扫码。</small>" : ""}<div class="card-actions">${action}</div></div>`;
    })
    .join("")}</div></section>`;
}

let s,
  page = "home",
  callback,
  fetching = false,
  toastTimer;
let mappings = null,
  mappingError = "",
  mappingRequest = 0;
const pages = {
  home: "工作台",
  jobs: "任务队列",
  history: "转发记录",
  mappings: "源帖映射",
  settings: "工作台设置",
};
function go(k) {
  const enteringMappings = k === "mappings" && page !== k;
  page = Object.hasOwn(pages, k) ? k : "home";
  location.hash = page;
  setNavigation(false);
  draw();
  if (enteringMappings) loadMappings(1);
}
window.addEventListener("hashchange", () => {
  if (signedIn) go(location.hash.slice(1));
});
const btn = (a, t, style = "", id = "") =>
  `<button type="button" class="btn ${style}" data-act="${a}" data-id="${esc(id)}">${t}</button>`;
const head = (t, extra = "") =>
  `<div class="cardhead"><h2>${t}</h2>${extra}</div>`;
const tag = (k, t) => `<span class="tag ${k}">${esc(t)}</span>`;
function draw() {
  if (!s) return;
  $("#version").textContent = s.version;
  $("#engine").textContent = "Bot 与网页共用队列";
  $("#nav").innerHTML = Object.entries(pages)
    .map(
      ([k, t]) =>
        `<button class="navitem ${k === page ? "active" : ""}" data-act="${k}">${ico(k)}${t}</button>`,
    )
    .join("");
  const { queue: q, config, history } = s;
  const card = (title, body, extra = "") =>
    `<section class="card">${head(title, extra)}${body}</section>`;
  const queueRows = [];
  function taskRow(status, job) {
    return `<tr><td>#${esc(job.job_id)}</td><td>${esc(status)}</td><td>${esc(job.link)}</td><td>${esc(channelLabel(job.target_chat_id))}</td></tr>`;
  }
  if (q.current_task) queueRows.push(taskRow("执行中", q.current_task));
  for (const [key, label] of [
    ["queued", "等待执行"],
    ["retry", "定时重试"],
    ["albums", "收集相册"],
    ["peer", "等待频道"],
    ["store", "等待数据库"],
  ]) {
    for (const job of q[key]) queueRows.push(taskRow(label, job));
  }
  function table(names, rows, empty) {
    return rows.length
      ? `<div class="tablebox"><table><thead><tr>${names.map((n) => `<th>${n}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`
      : `<p class="hint">${empty}</p>`;
  }
  const records = (rows) =>
    table(
      ["任务 / 记录", "来源", "发送路径", "结果", ""],
      rows.map(
        (r) =>
          `<tr><td>${r.job_id ? "#" + esc(r.job_id) + " / " : ""}${esc(r.id)}<small class="cellSub">${esc({ web: "网页", bot: "Bot", monitor: "自动监听" }[r.origin] || "")}</small></td><td>${esc(r.from)}</td><td>${esc(r.route || "旧记录")}</td><td>${tag(r.success ? "done" : r.unconfirmed ? "review" : "failed", r.success ? "已完成" : r.unconfirmed ? "待核对" : "失败")}</td><td>${btn("detail", "详情", "", r.id)}</td></tr>`,
      ),
      "尚无转发记录。",
    );
  const review = table(
    ["任务", "源帖", ""],
    s.reviews.map(
      (r) =>
        `<tr><td>#${esc(r.id)}</td><td>${esc(r.link)}</td><td>${btn("resolve", "标为已核对", "", r.id)}</td></tr>`,
    ),
    "没有待核对的任务。",
  );
  let body = "";
  if (page === "home") {
    body += card(
      "当前运行状态",
      `${pair("目标频道", channelLabel(config.target))}${pair("排队 / 待核对", `${q.counts.queued} / ${s.review_total}`)}${pair("当前任务", q.current_task ? "#" + q.current_task.job_id : "空闲")}`,
    );
    body += card("最近转发", records(history.slice(0, 6)));
  } else if (page === "jobs") {
    body +=
      card(
        "活动任务",
        table(["任务", "状态", "来源", "目标"], queueRows, "当前队列为空。"),
      ) + card(`待人工核对（${s.review_total}）`, review);
  } else if (page === "history") {
    body += card("最近 100 条转发记录", records(history));
  } else if (page === "mappings") {
    let content = mappingError ? `<p role="alert">${esc(mappingError)}</p>` : "";
    if (mappings) {
      const post = (channel, id, url) => {
        const label = `消息 #${esc(id)}`;
        const link = url
          ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${label}</a>`
          : label;
        return `${link}<small class="cellSub">${esc(channel)}</small>`;
      };
      const rows = mappings.rows.map((row) => {
        const source = post(row.source_label, row.source_message, row.source_url);
        const target = post(row.target_label, row.target_message, row.target_url);
        const confirmed = row.confirmed_at ? new Date(row.confirmed_at * 1000).toLocaleString() : "—";
        return `<tr><td>${source}</td><td>${target}</td><td>${esc(confirmed)}</td></tr>`;
      });
      const { page: current, pages: totalPages, total } = mappings;
      const previous = current > 1 ? btn("mappingPage", "上一页", "", current - 1) : "";
      const next = current < totalPages ? btn("mappingPage", "下一页", "", current + 1) : "";
      content += table(["源帖", "目标帖", "确认时间"], rows, "暂无已确认的源帖映射。");
      content += `<div class="actions">${previous}<span>第 ${current}/${totalPages} 页 · 共 ${total} 条</span>${next}</div>`;
    } else if (!mappingError) {
      content = '<p class="hint" role="status">正在读取源帖映射…</p>';
    }
    body += card("已确认映射", content, btn("mappingPage", "刷新", "", mappings?.page || 1));
  } else {
    body += card(
      "目标频道",
      pair("当前目标", channelLabel(config.target)),
      `<div class="actions">${config.target ? btn("rename", "编辑备注", "", config.target) : ""}${btn("target", "更改目标")}</div>`,
    );
    body += card(
      "监听源",
      s.sources.length
        ? s.sources
            .map(
              (id) =>
                `<div class="pair channel"><span>${esc(channelLabel(id))}</span><div class="actions">${btn("rename", "编辑备注", "", id)}${btn("removeSource", "移除", "", id)}</div></div>`,
            )
            .join("")
        : '<p class="hint">尚未配置监听源。</p>',
      btn("addSource", "添加来源"),
    );
    body += accountCards();
  }
  const title = page === "mappings" ? "自动复制的源帖映射" : pages[page];
  const description = page === "mappings"
    ? "查看已确认自动复制的源帖及其对应的目标帖。点击链接可打开对应消息。"
    : "请仅转发获授权的内容";
  $("#page").innerHTML =
    `<div class="rowhead"><div><h1>${title}</h1><div class="sub">${description}</div></div>${btn("new", "新建转发", "primary")}</div><div class="stack">${body}</div>`;
}
async function loadMappings(number) {
  const epoch = authEpoch;
  const request = ++mappingRequest;
  mappingError = "";
  try {
    const response = await fetch(`/api/mappings?page=${encodeURIComponent(number)}`, { cache: "no-store" });
    if (epoch !== authEpoch || request !== mappingRequest) return;
    if (response.status === 401) return loginScreen("会话已过期，请重新登录");
    if (!response.ok) throw Error();
    const result = await response.json();
    if (epoch !== authEpoch || request !== mappingRequest) return;
    mappings = result;
  } catch {
    if (epoch !== authEpoch || request !== mappingRequest) return;
    mappingError = "源帖映射暂不可用，请刷新重试。";
  }
  if (page === "mappings") draw();
}
async function refresh(force = false) {
  if (!signedIn || (fetching && !force)) return;
  const epoch = authEpoch;
  fetching = true;
  try {
    const res = await fetch("/api/state", { cache: "no-store" });
    if (epoch !== authEpoch) return;
    if (res.status === 401) return loginScreen("会话已过期，请重新登录");
    if (!res.ok) throw Error();
    const next = await res.json();
    if (epoch !== authEpoch) return;
    s = next;
    draw();
  } catch {
    $("#engine").textContent = "连接中断，正在重试";
  } finally {
    fetching = false;
  }
}
function newTask() {
  const target = s.config.target;
  if (!target) return handlers.target();
  const key = Array.from(crypto.getRandomValues(new Uint8Array(16)), (n) =>
    n.toString(16).padStart(2, "0"),
  ).join("");
  modal(
    "新建转发",
    `<div class="note">目标：${esc(channelLabel(target))} · 相册整组发送，?single 仅发选中成员。</div><label>任务类型<select name="kind" id="kind"><option value="link">链接或相册</option><option value="range">历史范围</option></select></label>${field("源帖链接", "src", "https://t.me/channel_name/123")}<label id="endRow" hidden>结束消息 ID<input name="end" inputmode="numeric"></label>`,
    "确认入队",
    async (p) => {
      const r = await act("submit", { ...p, target, key });
      notify("已入队 #" + r.id);
    },
  );
  $("#kind").onchange = (e) => {
    $("#endRow").hidden = e.target.value !== "range";
  };
}
function configureChannel(isTarget) {
  const previous = s.config.target;
  const title = isTarget ? "更改目标频道" : "添加监听源";
  const op = isTarget ? "target" : "add-source";
  const key = isTarget ? "target" : "source";
  const initial = isTarget && previous ? markedId(previous) : "";
  modal(
    title,
    field(
      "频道 ID、@用户名或链接",
      key,
      "https://t.me/c/1234567890/",
      initial,
    ) +
      `<small>支持公开频道、私有频道和原帖链接，不接受邀请链接。</small>`,
    "识别频道",
    async (p) => {
      const dialogGeneration = generation;
      const preview = await act("preview-channel", { channel: p[key] });
      if (generation !== dialogGeneration || !$("#dlg").open) return false;
      const id = preview.channel;
      const resolved = preview.title ? pair("频道名称", preview.title) : "";
      modal(
        title,
        resolved +
          pair("Telegram ID", markedId(id)) +
          pair("频道裸 ID", id) +
          '<p class="hint">已识别频道 ID，尚未验证监听或发帖权限。</p>' +
          nameField(
            preview.name || Array.from(preview.title).slice(0, 48).join(""),
          ),
        "确认保存",
        ({ name }) =>
          act(op, { [key]: id, name, ...(isTarget ? { previous } : {}) }),
      );
      return false;
    },
  );
}
const handlers = {
  mappingPage: (id) => loadMappings(id),
  new: () => newTask(),
  target: () => configureChannel(true),
  rename: (id) => {
    const previous = s.channel_names[id] || "";
    modal(
      "编辑频道备注",
      `<p>频道 ID：${esc(id)}</p>` +
        nameField(previous) +
        "<small>留空可清除备注。</small>",
      "保存",
      (p) => act("channel-name", { channel: id, name: p.name, previous }),
    );
  },
  addSource: () => configureChannel(false),
  removeSource: (id) =>
    modal(
      "移除监听源",
      `<p>停止监听 ${esc(channelLabel(id))}，不会删除已有帖子。</p>`,
      "移除",
      () => act("remove-source", { source: id }),
    ),
  resolve: (id) =>
    modal(
      "确认已核对",
      `<p>请先检查目标频道。移除 #${esc(id)} 的待核对标记不会重发消息。</p>`,
      "已核对",
      () => act("resolve", { id }),
    ),
  detail: (id) => {
    const r = s.history.find((x) => String(x.id) === id);
    if (!r) return;
    modal(
      "转发记录 " + id,
      `<p>${esc(r.from)} → ${esc(channelLabel(r.to))}</p><p>${esc(r.route || "")} ${esc(r.error || "")}</p>` +
        (r.album_groups || [])
          .map((ids) => pair("源相册分组", ids.join("、")))
          .join("") +
        (r.members || [])
          .map(([src, dst]) =>
            pair(
              "源消息 " + src,
              dst > 0 ? "目标消息 " + dst : dst === 0 ? "待核对" : "未发送",
            ),
          )
          .join(""),
      null,
      null,
    );
  },
  tdlLogin: () => openLogin("tdl"),
  telethonLogin: () => openLogin("telethon"),
};
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-act]");
  if (!b) return;
  const a = b.dataset.act;
  if (Object.hasOwn(pages, a)) go(a);
  else if (Object.hasOwn(handlers, a)) handlers[a](b.dataset.id);
});
let loginTicket = null,
  generation = 0;
async function authPost(op, p) {
  let r = await fetch("/api/login/" + op, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Relay-Request": "1",
      "X-CSRF-Token": csrf,
    },
    body: JSON.stringify(p),
  });
  let obj = await r.json();
  if (r.status === 401) {
    loginScreen("会话已过期，请重新登录。");
    throw Error("请重新登录");
  }
  if (!r.ok) throw Error(obj.error || "登录请求失败");
  return obj;
}
function openLogin(provider) {
  const tele = provider === "telethon",
    replace = Boolean(s.accounts?.[provider]),
    title =
      (tele ? "Telethon" : "TDL") + (replace ? " 重新扫码登录" : " 扫码登录");
  const steps =
    "<p>在已登录的 Telegram 手机上打开「设置 → 设备 → 连接桌面设备」，扫描本窗口的二维码并确认。</p>";
  const my = ++generation;
  const applicationFields = tele
    ? `<details><summary>使用其他 Telegram 应用凭据（可选）</summary>
       <p class="hint">默认使用${replace ? "当前会话保存的凭据" : "容器配置的凭据"}。如需更换，请同时填写两项。</p>
       <label>API ID<input name="api_id" inputmode="numeric" pattern="[1-9][0-9]{0,9}" autocomplete="off"></label>
       <label>API Hash<input name="api_hash" type="password" pattern="[0-9A-Fa-f]{32}" autocomplete="off"></label></details>`
    : "";
  modal(
    title,
    (replace
      ? '<p class="note">新账号验证成功后才替换原会话，取消、超时或失败均保留原凭据。建议使用原 Telegram 账号重新授权，以保留频道权限和编辑能力。</p><p class="hint">如有任务正在执行，请先暂停队列并等待任务结束；保存时会等待当前任务完成。</p>'
      : "") +
      steps +
      applicationFields,
    replace ? "确认并生成二维码" : "生成二维码",
    async (p) => {
      const { flow } = await authPost("start", { provider, replace, ...p });
      if (my !== generation || !$("#dlg").open) {
        await authPost("cancel", { flow });
        return false;
      }
      loginTicket = flow;
      modal(title, "<p>正在连接 Telegram…</p>", null, null);
      pollLogin(my, flow, title);
      return false;
    },
  );
}
async function pollLogin(my, flow, title) {
  let last = "";
  while (my === generation && $("#dlg").open) {
    try {
      const v = await authPost("poll", { flow });
      if (my !== generation) return;
      if (v.phase === "scan") {
        if (last !== "scan")
          modal(
            title,
            '<p>打开手机 Telegram → 设置 → 设备，扫码并确认。</p><canvas class="authqr" aria-label="Telegram 设备登录二维码"></canvas><small id="qrtime"></small>',
            null,
            null,
          );
        const c = $(".authqr"),
          n = v.qr.length;
        c.width = c.height = n * 6;
        const g = c.getContext("2d");
        g.fillStyle = "#fff";
        g.fillRect(0, 0, c.width, c.height);
        g.fillStyle = "#111";
        v.qr.forEach((row, y) =>
          [...row].forEach((b, x) => {
            if (b === "1") g.fillRect(x * 6, y * 6, 6, 6);
          }),
        );
        $("#qrtime").textContent = `会话剩余 ${v.remaining} 秒 · 请勿分享截图`;
      } else if (v.phase === "saving" && last !== "saving") {
        modal(
          title,
          "<p>验证成功，正在安全保存会话。若有任务正在执行，将等待其结束；请勿关闭窗口。</p>",
          null,
          null,
        );
      } else if (v.phase === "password" && last !== "password") {
        modal(
          title,
          '<label>Telegram 两步验证密码<input name="password" type="password" required maxlength="256" autocomplete="off"></label><small>仅用于本次验证，不会保存。</small>',
          "验证",
          async (p) => {
            await authPost("password", { flow, password: p.password });
            modal(title, "<p>正在验证…</p>", null, null);
            return false;
          },
        );
      } else if (["done", "expired", "failed", "cancelled"].includes(v.phase)) {
        loginTicket = null;
        modal(
          title,
          `<p>${esc(v.error || { done: title.includes("Telethon") ? "会话已保存，正在自动启动监听。请查看工作台设置中的监听状态。" : "已授权，会话已保存在本容器的数据卷。", expired: "登录已超时，请重新开始。", cancelled: "登录已取消。" }[v.phase])}</p>`,
          null,
          null,
        );
        await refresh(true);
        return;
      }
      last = v.phase;
    } catch (e) {
      if (my === generation) {
        modal(title, `<p>${esc(e.message)}。关闭后重试。</p>`, null, null);
      }
      return;
    }
    await new Promise((r) => setTimeout(r, 800));
  }
}
$("#dlg").addEventListener("close", () => {
  generation++;
  if (loginTicket) {
    authPost("cancel", { flow: loginTicket }).catch(() => {});
    loginTicket = null;
  }
  $("#fields").replaceChildren();
  callback = null;
});
$("#form").onsubmit = async (e) => {
  e.preventDefault();
  if (!callback) return;
  $("#submit").disabled = true;
  try {
    const keepOpen = await callback(Object.fromEntries(new FormData(e.target)));
    if (keepOpen !== false) $("#dlg").close();
  } catch (err) {
    $("#error").textContent = err.message;
  } finally {
    $("#submit").disabled = false;
  }
};
$("#back").onclick = $("#close").onclick = () => $("#dlg").close();
function setNavigation(open) {
  document.body.classList.toggle("open", open);
  $("#menu").setAttribute("aria-expanded", String(open));
  $("#menu").setAttribute("aria-label", open ? "关闭导航" : "打开导航");
}
$("#menu").onclick = () =>
  setNavigation(!document.body.classList.contains("open"));
$("#shade").onclick = () => setNavigation(false);
document.addEventListener("keydown", (event) => {
  if (
    event.key === "Escape" &&
    document.body.classList.contains("open") &&
    !$("#dlg").open
  ) {
    setNavigation(false);
    $("#menu").focus();
  }
});
let csrf = "",
  signedIn = false,
  authEpoch = 0;
function loginScreen(message = "") {
  signedIn = false;
  authEpoch++;
  csrf = "";
  s = null;
  page = "home";
  mappings = null;
  mappingError = "";
  setNavigation(false);
  if ($("#dlg").open) $("#dlg").close();
  $("#workspace").hidden = true;
  $("#gate").hidden = false;
  $("#page").replaceChildren();
  $("#nav").replaceChildren();
  $("#pwd").value = "";
  $("#pwd").type = "password";
  $("#see").textContent = "显示";
  $("#see").setAttribute("aria-pressed", "false");
  $("#loginerr").textContent = message;
}
async function enter() {
  signedIn = true;
  authEpoch++;
  $("#pwd").value = "";
  $("#gate").hidden = true;
  $("#workspace").hidden = false;
  go(location.hash.slice(1));
  await refresh(true);
}
async function boot() {
  try {
    const r = await fetch("/api/auth/session", { cache: "no-store" });
    if (!r.ok) throw Error();
    const v = await r.json();
    if (!v.authenticated) return loginScreen();
    csrf = v.csrf || "";
    await enter();
  } catch {
    loginScreen("无法连接服务器，请检查连接后重试。");
  }
}
$("#signin").onsubmit = async (e) => {
  e.preventDefault();
  const b = e.submitter || $("#signin .enter");
  b.disabled = true;
  try {
    const r = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Relay-Request": "1" },
      body: JSON.stringify({
        username: $("#uname").value.trim(),
        password: $("#pwd").value,
        remember: $("#keep").checked,
      }),
    });
    const v = await r.json();
    if (!r.ok) throw Error(v.error || "登录失败");
    csrf = v.csrf;
    await enter();
  } catch (err) {
    $("#loginerr").textContent = err.message;
  } finally {
    b.disabled = false;
  }
};
$("#see").onclick = () => {
  const show = $("#pwd").type === "password";
  $("#pwd").type = show ? "text" : "password";
  $("#see").textContent = show ? "隐藏" : "显示";
  $("#see").setAttribute("aria-pressed", String(show));
};
$("#forgot").onclick = () => {
  $("#loginerr").textContent =
    "通过已信任的 SSH 运行 docker logs 容器名，查找 Web password。";
};
$("#out").onclick = async () => {
  try {
    if (loginTicket) {
      await authPost("cancel", { flow: loginTicket }).catch(() => {});
      loginTicket = null;
    }
    const r = await fetch("/api/auth/logout", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Relay-Request": "1",
        "X-CSRF-Token": csrf,
      },
      body: "{}",
    });
    if (!r.ok && r.status !== 401) throw Error();
    loginScreen("已退出登录。");
  } catch {
    notify("退出失败，请重试。");
  }
};
boot();
setInterval(() => {
  if (signedIn && !document.hidden) refresh();
}, 2500);
