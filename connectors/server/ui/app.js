"use strict";
// MyAgent Connectors — admin UI (vanilla JS, no dependencies).

const $ = (id) => document.getElementById(id);
const TOKEN_MASK = "********";
let editing = null;   // id being edited, or null for a new binding
let agents = [];      // [{id,name}]

async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const resp = await fetch("/api/bindings" + path, opt);
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!resp.ok) throw new Error((data && data.detail) || resp.statusText);
  return data;
}

function toast(msg, kind = "ok") {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast show " + kind;
  setTimeout(() => (t.className = "toast"), 2600);
}

// ------------------------------------------------------------- static i18n
// Translate elements carrying data-i18n (textContent) / data-i18n-ph
// (placeholder). Exposed globally so I18n.setLocale can re-apply it.
window.applyStaticI18n = function () {
  document.title = i18n("app.title");
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = i18n(el.getAttribute("data-i18n"));
  });
  document.querySelectorAll("[data-i18n-ph]").forEach((el) => {
    el.setAttribute("placeholder", i18n(el.getAttribute("data-i18n-ph")));
  });
};

// ---------------------------------------------------------------- list view
function statusBadge(b) {
  if (!b.enabled) return `<span class="badge disabled"><span class="dot"></span>${i18n("status.disabled")}</span>`;
  const st = (b.status && b.status.state) || "stopped";
  const detail = (b.status && b.status.detail) || "";
  const label = i18n("status." + st);
  const extra = detail ? ` · ${detail}` : "";
  return `<span class="badge ${st}"><span class="dot"></span>${label}${extra}</span>`;
}

async function refresh() {
  let list;
  try { list = await api("GET", ""); }
  catch (e) { $("list").innerHTML = `<div class="empty">${i18n("list.error", { msg: e.message })}</div>`; return; }
  const el = $("list");
  if (!list.length) {
    el.innerHTML = `<div class="empty">${i18n("list.empty")}</div>`;
    return;
  }
  el.innerHTML = "";
  for (const b of list) {
    const div = document.createElement("div");
    div.className = "binding";
    const msgs = (b.status && b.status.messages) || 0;
    div.innerHTML = `
      <div class="grow">
        <div class="name">${b.name || b.id}</div>
        <div class="sub">${b.type} · ${b.agent_id || "—"} · ${b.access_mode} · ${msgs} ${i18n("list.msgs")}</div>
      </div>
      ${statusBadge(b)}`;
    div.onclick = () => openEdit(b.id);
    el.appendChild(div);
  }
}

// ---------------------------------------------------------------- edit modal
function setAccessVisibility() {
  const mode = $("f-access").value;
  $("wrap-allowed").classList.toggle("hidden", mode !== "allowlist");
  $("wrap-password").classList.toggle("hidden", mode !== "password");
}

function fillForm(b) {
  $("f-id").value = b.id || "";
  $("f-id").disabled = !!editing;
  $("f-name").value = b.name || "";
  $("f-type").value = b.type || "telegram";
  $("f-agent").value = b.agent_id || (agents[0] && agents[0].id) || "";
  $("f-token").value = b.token === TOKEN_MASK ? TOKEN_MASK : (b.token || "");
  $("f-access").value = b.access_mode || "allowlist";
  $("f-prefix").value = b.session_prefix || "";
  // Show numeric ids and @usernames together in one comma-separated field.
  $("f-allowed").value = (b.allowed_ids || []).map(String)
    .concat((b.allowed_usernames || []).map((u) => "@" + u)).join(", ");
  $("f-password").value = b.password === TOKEN_MASK ? TOKEN_MASK : (b.password || "");
  $("f-welcome").value = b.welcome || "";
  $("f-help").value = b.help_text || "";
  $("f-enabled").checked = b.enabled !== false;
  $("test-note").textContent = i18n("note.token");
  setAccessVisibility();
}

async function openEdit(id) {
  editing = id;
  const b = await api("GET", "/" + id);
  $("modal-title").textContent = i18n("modal.editTitle");
  $("btn-delete").classList.remove("hidden");
  fillForm(b);
  $("overlay").classList.add("open");
}

function openNew() {
  editing = null;
  $("modal-title").textContent = i18n("modal.newTitle");
  $("btn-delete").classList.add("hidden");
  fillForm({ type: $("f-type").value || "telegram", access_mode: "allowlist", enabled: true });
  $("overlay").classList.add("open");
}

function closeModal() { $("overlay").classList.remove("open"); }

function collectForm() {
  // The authorized-users field mixes numeric ids and @usernames.
  const allowed_ids = [];
  const allowed_usernames = [];
  $("f-allowed").value.split(",").map((s) => s.trim()).filter(Boolean).forEach((tok) => {
    if (/^-?\d+$/.test(tok)) allowed_ids.push(Number(tok));
    else allowed_usernames.push(tok.replace(/^@/, "").toLowerCase());
  });
  return {
    id: $("f-id").value.trim(),
    name: $("f-name").value.trim(),
    type: $("f-type").value,
    enabled: $("f-enabled").checked,
    agent_id: $("f-agent").value,
    token: $("f-token").value.trim(),
    access_mode: $("f-access").value,
    allowed_ids: allowed_ids,
    allowed_usernames: allowed_usernames,
    password: $("f-password").value,
    session_prefix: $("f-prefix").value.trim(),
    welcome: $("f-welcome").value,
    help_text: $("f-help").value,
  };
}

async function save() {
  const b = collectForm();
  if (!b.id) return toast(i18n("validate.idRequired"), "err");
  if (!b.agent_id) return toast(i18n("validate.agentRequired"), "err");
  $("btn-save").disabled = true;
  try {
    if (editing) await api("PUT", "/" + editing, b);
    else await api("POST", "", b);
    toast(i18n("toast.saved"));
    closeModal();
    refresh();
  } catch (e) { toast(e.message, "err"); }
  finally { $("btn-save").disabled = false; }
}

async function del() {
  if (!editing) return;
  if (!confirm(i18n("confirm.delete", { id: editing }))) return;
  try { await api("DELETE", "/" + editing); toast(i18n("toast.deleted")); closeModal(); refresh(); }
  catch (e) { toast(e.message, "err"); }
}

async function testToken() {
  const token = $("f-token").value.trim();
  const type = $("f-type").value;
  $("btn-test").disabled = true;
  $("test-note").textContent = i18n("test.checking");
  try {
    let res;
    if (token === TOKEN_MASK && editing) res = await api("POST", "/" + editing + "/test");
    else res = await api("POST", "/test", { type, token });
    $("test-note").textContent = i18n("test.valid", { bot: res.bot, name: res.name || "" });
  } catch (e) { $("test-note").textContent = i18n("test.fail", { msg: e.message }); }
  finally { $("btn-test").disabled = false; }
}

// ---------------------------------------------------------------- bootstrap
async function loadOptions() {
  try {
    const types = (await api("GET", "/types")).types || ["telegram"];
    $("f-type").innerHTML = types.map((t) => `<option value="${t}">${t}</option>`).join("");
  } catch { $("f-type").innerHTML = `<option value="telegram">telegram</option>`; }
  try {
    agents = await api("GET", "/agents");
    $("f-agent").innerHTML = agents.map((a) => `<option value="${a.id}">${a.name}</option>`).join("");
    $("ma-dot").className = "dot ok"; $("ma-url").textContent = i18n("header.connected");
  } catch (e) {
    $("f-agent").innerHTML = `<option value="">${i18n("agent.unreachable")}</option>`;
    $("ma-dot").className = "dot err"; $("ma-url").textContent = i18n("header.offline");
  }
}

$("btn-new").onclick = openNew;
$("btn-cancel").onclick = closeModal;
$("btn-save").onclick = save;
$("btn-delete").onclick = del;
$("btn-test").onclick = testToken;
$("f-access").onchange = setAccessVisibility;
$("overlay").onclick = (e) => { if (e.target === $("overlay")) closeModal(); };

// i18n bootstrap: pick locale, translate static chrome, then load data.
I18n.init();
$("lang").value = I18n.locale;
$("lang").onchange = (e) => I18n.setLocale(e.target.value);
window.onLocaleChange = () => { loadOptions().then(refresh); };  // refresh dynamic text
applyStaticI18n();

loadOptions().then(refresh);
setInterval(refresh, 5000);  // live status
