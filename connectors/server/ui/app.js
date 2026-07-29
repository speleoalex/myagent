"use strict";
// MyAgent Connectors — admin UI (vanilla JS, no dependencies).

const $ = (id) => document.getElementById(id);
const TOKEN_MASK = "********";
let editing = null;          // id being edited, or null for a new binding
let agents = [];             // [{id,name}]
let contacts = [];           // address book: [{id,name,user_id,username,notes}]
let editingContact = null;   // contact id being edited, or null for a new one

async function http(method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const resp = await fetch(url, opt);
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!resp.ok) throw new Error((data && data.detail) || resp.statusText);
  return data;
}
const api = (method, path, body) => http(method, "/api/bindings" + path, body);
const capi = (method, path, body) => http(method, "/api/contacts" + path, body);

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

// ------------------------------------------------------------- address book
// A contact's identifier as it appears in the authorized-users field: the
// permanent numeric id when known, the @username otherwise.
function contactIdentifier(c) {
  if (c.user_id !== null && c.user_id !== undefined) return String(c.user_id);
  if (c.username) return "@" + c.username;
  return "";
}

async function refreshContacts() {
  try { contacts = await capi("GET", ""); }
  catch (e) { $("contacts").innerHTML = `<div class="empty">${i18n("list.error", { msg: e.message })}</div>`; return; }
  const el = $("contacts");
  if (!contacts.length) {
    el.innerHTML = `<div class="empty">${i18n("contacts.empty")}</div>`;
    return;
  }
  el.innerHTML = "";
  for (const c of contacts) {
    const div = document.createElement("div");
    div.className = "binding";
    const parts = [];
    if (c.user_id !== null && c.user_id !== undefined) parts.push(String(c.user_id));
    if (c.username) parts.push("@" + c.username);
    if (c.notes) parts.push(c.notes);
    div.innerHTML = `
      <div class="grow">
        <div class="name">${c.name || c.id}</div>
        <div class="sub">${parts.join(" · ") || "—"}</div>
      </div>`;
    div.onclick = () => openContactEdit(c.id);
    el.appendChild(div);
  }
}

function fillContactForm(c) {
  $("c-id").value = c.id || "";
  $("c-id").disabled = !!editingContact;
  $("c-name").value = c.name || "";
  $("c-userid").value = c.user_id === null || c.user_id === undefined ? "" : String(c.user_id);
  $("c-username").value = c.username ? "@" + c.username : "";
  $("c-notes").value = c.notes || "";
}

async function openContactEdit(id) {
  editingContact = id;
  const c = await capi("GET", "/" + id);
  $("contact-modal-title").textContent = i18n("contacts.modal.editTitle");
  $("btn-contact-delete").classList.remove("hidden");
  fillContactForm(c);
  $("overlay-contact").classList.add("open");
}

function openContactNew() {
  editingContact = null;
  $("contact-modal-title").textContent = i18n("contacts.modal.newTitle");
  $("btn-contact-delete").classList.add("hidden");
  fillContactForm({});
  $("overlay-contact").classList.add("open");
}

function closeContactModal() { $("overlay-contact").classList.remove("open"); }

// Derive a valid record id from the name (ids must start alphanumeric and
// only contain [A-Za-z0-9._-], same charset the server validates).
function slugify(s) {
  return s.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^[^a-z0-9]+/, "").replace(/[-._]+$/, "");
}

function collectContactForm() {
  const name = $("c-name").value.trim();
  const userIdRaw = $("c-userid").value.trim();
  return {
    id: $("c-id").value.trim() || slugify(name),
    name,
    user_id: /^-?\d+$/.test(userIdRaw) ? Number(userIdRaw) : null,
    username: $("c-username").value.trim().replace(/^@/, "").toLowerCase(),
    notes: $("c-notes").value,
  };
}

async function saveContact() {
  const userIdRaw = $("c-userid").value.trim();
  if (userIdRaw && !/^-?\d+$/.test(userIdRaw)) return toast(i18n("contacts.validate.userIdNumeric"), "err");
  const c = collectContactForm();
  if (!c.name) return toast(i18n("contacts.validate.nameRequired"), "err");
  if (!c.id) return toast(i18n("validate.idRequired"), "err");
  if (c.user_id === null && !c.username) return toast(i18n("contacts.validate.identifierRequired"), "err");
  $("btn-contact-save").disabled = true;
  try {
    if (editingContact) await capi("PUT", "/" + editingContact, c);
    else await capi("POST", "", c);
    toast(i18n("toast.saved"));
    closeContactModal();
    await refreshContacts();
    renderAllowedChips();  // the binding form may be open behind the modal
  } catch (e) { toast(e.message, "err"); }
  finally { $("btn-contact-save").disabled = false; }
}

async function delContact() {
  if (!editingContact) return;
  if (!confirm(i18n("contacts.confirm.delete", { id: editingContact }))) return;
  try {
    await capi("DELETE", "/" + editingContact);
    toast(i18n("toast.deleted"));
    closeContactModal();
    await refreshContacts();
    renderAllowedChips();
  } catch (e) { toast(e.message, "err"); }
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
  renderAllowedChips();
}

// ------------------------------------------------- address-book chips
// One chip per contact under the authorized-users field; clicking toggles the
// contact's identifier in and out of the comma-separated value. Chips only
// mirror the field — the field stays the single source of truth on save.
function allowedTokens() {
  return $("f-allowed").value.split(",").map((s) => s.trim()).filter(Boolean);
}

// A token matches a contact by numeric id OR by @username (case-insensitive).
function tokenMatchesContact(tok, c) {
  if (c.user_id !== null && c.user_id !== undefined && tok === String(c.user_id)) return true;
  return !!c.username && tok.toLowerCase() === "@" + c.username;
}

function renderAllowedChips() {
  const el = $("allowed-contacts");
  const tokens = allowedTokens();
  el.innerHTML = "";
  for (const c of contacts) {
    const ident = contactIdentifier(c);
    if (!ident) continue;
    const chip = document.createElement("span");
    const on = tokens.some((t) => tokenMatchesContact(t, c));
    chip.className = "chip" + (on ? " on" : "");
    chip.textContent = c.name || c.id;
    chip.title = ident;
    chip.onclick = () => {
      const now = allowedTokens();
      const next = now.some((t) => tokenMatchesContact(t, c))
        ? now.filter((t) => !tokenMatchesContact(t, c))
        : now.concat([ident]);
      $("f-allowed").value = next.join(", ");
      renderAllowedChips();
    };
    el.appendChild(chip);
  }
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
$("f-allowed").oninput = renderAllowedChips;  // typing an id lights its chip
$("btn-new-contact").onclick = openContactNew;
$("btn-contact-cancel").onclick = closeContactModal;
$("btn-contact-save").onclick = saveContact;
$("btn-contact-delete").onclick = delContact;
// Close on outside click, but only if the press ALSO started on the overlay:
// selecting text in an input and releasing outside the modal fires a click on
// the overlay (common ancestor), which must not dismiss the form.
function wireOverlayDismiss(ov, close) {
  let pressedOutside = false;
  ov.onmousedown = (e) => { pressedOutside = e.target === ov; };
  ov.onclick = (e) => {
    if (e.target === ov && pressedOutside) close();
    pressedOutside = false;
  };
}
wireOverlayDismiss($("overlay"), closeModal);
wireOverlayDismiss($("overlay-contact"), closeContactModal);

// i18n bootstrap: pick locale, translate static chrome, then load data.
I18n.init();
$("lang").value = I18n.locale;
$("lang").onchange = (e) => I18n.setLocale(e.target.value);
window.onLocaleChange = () => { loadOptions().then(refresh); refreshContacts(); };  // refresh dynamic text
applyStaticI18n();

loadOptions().then(refresh);
refreshContacts();
setInterval(refresh, 5000);  // live status (contacts have none — no polling)
