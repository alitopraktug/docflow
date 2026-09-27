"use strict";

// ---------------------------------------------------------------- helpers

const $ = (sel, root = document) => root.querySelector(sel);
const app = $("#app");
let token = localStorage.getItem("docflow.token");
let pollTimer = null;

const STATUS = {
  processing: "İşleniyor",
  needs_review: "Kontrol gerekli",
  ready: "Hazır",
  approved: "Onaylandı",
  failed: "Başarısız",
  auto_accepted: "Otomatik",
  corrected: "Düzeltildi",
  confirmed: "Onaylandı",
};
const ISSUES = {
  not_found: "Belgede bulunamadı",
  value_unreadable: "Etiket bulundu ama değer okunamadı",
  low_confidence: "OCR güveni düşük",
  ocr_char_substitution: "Harf/rakam karışıklığı düzeltildi (ör. O→0)",
  ambiguous_characters: "Birbirine benzeyen harf/rakam var (ör. S/5, O/0)",
  malformed_number: "Sayı biçimi bozuk",
  not_a_number: "Sayı okunamadı",
  not_a_date: "Tarih okunamadı",
  invalid_date: "Geçersiz tarih",
  not_a_tax_id: "Vergi numarası okunamadı",
  not_a_code: "Belge numarası okunamadı",
  checksum_failed: "VKN kontrol hanesi tutmuyor",
  row_arithmetic_mismatch: "Miktar × birim fiyat ≠ tutar",
  lines_do_not_sum_to_subtotal: "Kalemlerin toplamı ara toplamı tutmuyor",
  tax_mismatch: "Ara toplam × KDV oranı ≠ KDV tutarı",
  total_mismatch: "Ara toplam + KDV ≠ genel toplam",
  due_before_invoice_date: "Vade tarihi belge tarihinden önce",
  date_in_future: "Tarih ileride",
  negative_value: "Negatif değer",
  unexpected_character: "Beklenmeyen karakter",
  conflicting_currency: "Belgede farklı para birimleri var",
  learned_from_correction: "Önceki düzeltmenizden öğrenildi",
};
const ACTIONS = { correct: "Düzeltme", confirm: "Onay", add_row: "Satır ekleme", delete_row: "Satır silme" };
const NUMERIC = new Set(["quantity", "unit_price", "amount"]);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function toast(msg, ms = 3000) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), ms);
}

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.json);
  }
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) {
    logout();
    throw new Error("Oturum geçersiz");
  }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Hata ${res.status}`);
  return data;
}

function withToken(url) {
  return `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}`;
}

function fmtMs(ms) {
  if (ms == null) return "–";
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} sn`;
}

function fmtValue(name, value) {
  if (value == null) return "";
  if (["subtotal", "tax_amount", "total", "unit_price", "amount"].includes(name)) {
    const n = Number(value);
    return Number.isFinite(n) ? n.toLocaleString("tr-TR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : value;
  }
  if (name === "quantity") return Number(value).toLocaleString("tr-TR");
  if (/date/.test(name) && /^\d{4}-\d{2}-\d{2}$/.test(value)) {
    const [y, m, d] = value.split("-");
    return `${d}.${m}.${y}`;
  }
  return value;
}

// ---------------------------------------------------------------- routing

function logout() {
  localStorage.removeItem("docflow.token");
  token = null;
  location.hash = "#/";
  route();
}

async function route() {
  clearInterval(pollTimer);
  if (!token) return renderLogin();
  const me = await api("/api/me").catch(() => null);
  if (!me) return;
  $("#user").innerHTML = `<span>${esc(me.name)}</span><button class="link" id="logout">Çıkış</button>`;
  $("#logout").onclick = logout;
  refreshStats();
  const m = location.hash.match(/^#\/doc\/(.+)$/);
  if (m) return renderDetail(m[1]);
  return renderList();
}
window.addEventListener("hashchange", route);

async function refreshStats() {
  const s = await api("/api/stats").catch(() => null);
  if (!s) return;
  const pct = (x) => `%${(x * 100).toFixed(1)}`;
  $("#stats").innerHTML = `
    <span class="stat">Belge <b>${s.documents}</b></span>
    <span class="stat">Ort. işlem <b>${fmtMs(s.avg_processing_ms)}</b></span>
    <span class="stat" title="Sistemin emin olamayıp kullanıcıya bıraktığı alanlar">Kontrole düşen <b>${pct(s.review_rate)}</b></span>
    <span class="stat" title="Kullanıcının değerini değiştirdiği alanlar">Düzeltilen <b>${pct(s.correction_rate)}</b></span>`;
}

// ---------------------------------------------------------------- login

function renderLogin() {
  $("#user").innerHTML = "";
  $("#stats").innerHTML = "";
  app.replaceChildren($("#tpl-login").content.cloneNode(true));
  $("#login-form").onsubmit = async (e) => {
    e.preventDefault();
    const name = new FormData(e.target).get("name");
    const user = await api("/api/auth/register", { method: "POST", json: { name } });
    token = user.token;
    localStorage.setItem("docflow.token", token);
    route();
  };
}

// ---------------------------------------------------------------- list

async function renderList() {
  app.replaceChildren($("#tpl-list").content.cloneNode(true));
  const input = $("#file-input");
  const zone = $("#dropzone");
  input.onchange = () => upload([...input.files]);
  zone.ondragover = (e) => { e.preventDefault(); zone.classList.add("over"); };
  zone.ondragleave = () => zone.classList.remove("over");
  zone.ondrop = (e) => { e.preventDefault(); zone.classList.remove("over"); upload([...e.dataTransfer.files]); };
  await loadList();
  pollTimer = setInterval(loadList, 1000);
}

async function upload(files) {
  for (const file of files) {
    const body = new FormData();
    body.append("file", file);
    try {
      const res = await api("/api/documents", { method: "POST", body });
      if (res.duplicate) {
        toast(`"${file.name}" daha önce yüklenmiş; mevcut belge gösteriliyor.`, 4000);
        if (files.length === 1) location.hash = `#/doc/${res.document.id}`;
      } else {
        toast(`"${file.name}" sıraya alındı`);
      }
    } catch (err) {
      toast(`${file.name}: ${err.message}`, 5000);
    }
  }
  loadList();
}

async function loadList() {
  const tbody = $("#doc-rows");
  if (!tbody) return;
  const docs = await api("/api/documents");
  $("#empty").hidden = docs.length > 0;
  tbody.innerHTML = docs.map((d) => {
    const job = d.job || {};
    const running = d.status === "processing";
    const statusCell = running
      ? `<span class="badge processing">${esc(job.message || "İşleniyor")}</span>
         <div class="progress"><i style="width:${Math.round((job.progress || 0) * 100)}%"></i></div>`
      : `<span class="badge ${d.status}">${STATUS[d.status] || d.status}</span>`;
    const fc = d.field_counts;
    const review = running ? "" : fc.pending ? `${fc.pending} / ${fc.total} alan` : fc.total ? "Tamam" : "";
    return `<tr class="clickable" data-id="${d.id}">
      <td><strong>${esc(d.filename)}</strong><div class="muted small">${d.page_count ?? "?"} sayfa · ${d.source_kind === "pdf_text" ? "PDF metni" : d.source_kind === "ocr" ? "OCR" : ""}${d.possible_duplicate_of ? " · olası mükerrer" : ""}</div></td>
      <td>${statusCell}${job.status === "failed" ? `<div class="small" style="color:var(--bad)">${esc((job.error || "").split("\n").filter(Boolean).pop() || "")}</div>` : ""}</td>
      <td>${review}</td>
      <td>${fmtMs(d.processing_ms)}</td>
      <td class="muted">${new Date(d.created_at * 1000).toLocaleString("tr-TR")}</td>
      <td>${job.status === "failed" ? `<button data-retry="${d.id}">Yeniden dene</button>` : ""}
          <button class="link danger" data-del="${d.id}" title="Sil">Sil</button></td>
    </tr>`;
  }).join("");
  tbody.onclick = async (e) => {
    const retry = e.target.dataset.retry;
    const del = e.target.dataset.del;
    if (retry) { e.stopPropagation(); await api(`/api/documents/${retry}/retry`, { method: "POST" }); return loadList(); }
    if (del) {
      e.stopPropagation();
      if (confirm("Belge ve çıkarılan veriler silinsin mi?")) { await api(`/api/documents/${del}`, { method: "DELETE" }); loadList(); refreshStats(); }
      return;
    }
    const tr = e.target.closest("tr[data-id]");
    if (tr) location.hash = `#/doc/${tr.dataset.id}`;
  };
  if (!docs.some((d) => d.status === "processing")) refreshStats();
}

// ---------------------------------------------------------------- detail

let doc = null;
let zoom = 1;
let activeId = null;

async function renderDetail(id) {
  app.replaceChildren($("#tpl-detail").content.cloneNode(true));
  zoom = 1;
  activeId = null;
  await loadDetail(id);
  $("#zoom-in").onclick = () => setZoom(zoom * 1.25);
  $("#zoom-out").onclick = () => setZoom(zoom / 1.25);
  $("#only-review").onchange = (e) => $(".panel").classList.toggle("hide-ok", e.target.checked);
  $("#next-review").onclick = nextReview;
  $("#approve").onclick = async () => {
    try { setDoc(await api(`/api/documents/${doc.id}/approve`, { method: "POST" })); toast("Belge onaylandı"); refreshStats(); }
    catch (err) { toast(err.message); }
  };
  $("#add-row").onclick = async () => {
    setDoc(await api(`/api/documents/${doc.id}/rows`, { method: "POST", json: {} }));
    const last = doc.rows[doc.rows.length - 1];
    if (last) focusField(last.cells.description.id);
  };
}

async function loadDetail(id) {
  const d = await api(`/api/documents/${id}`);
  setDoc(d);
  if (d.status === "processing") {
    pollTimer = setInterval(async () => {
      const next = await api(`/api/documents/${id}`);
      if (next.status !== "processing") { clearInterval(pollTimer); setDoc(next); refreshStats(); }
      else renderBanner(next);
    }, 1000);
  }
}

function setDoc(d) {
  doc = d;
  $("#doc-title").textContent = d.filename;
  const st = $("#doc-status");
  st.className = `badge ${d.status}`;
  st.textContent = STATUS[d.status] || d.status;
  $("#dl-csv").href = withToken(`/api/documents/${d.id}/export.csv?sep=;`);
  $("#dl-xlsx").href = withToken(`/api/documents/${d.id}/export.xlsx`);
  const pending = d.field_counts.pending;
  $("#approve").disabled = pending > 0 || d.status === "processing" || d.status === "approved";
  $("#approve").title = pending ? `${pending} alan kontrol bekliyor` : "";
  $("#next-review").disabled = !pending;
  renderBanner(d);
  renderPages(d);
  renderHeader(d);
  renderLines(d);
  loadHistory(d.id);
}

function renderBanner(d) {
  const parts = [];
  const job = d.job || {};
  if (d.status === "processing") {
    parts.push(`<div class="warn">${esc(job.message || "İşleniyor")} · %${Math.round((job.progress || 0) * 100)}${job.attempts > 1 ? ` · ${job.attempts}. deneme, kaldığı yerden devam ediyor` : ""}</div>`);
  }
  if (d.status === "failed") parts.push(`<div class="warn err">İşlem başarısız oldu. Belgeler listesinden yeniden deneyebilirsiniz; tamamlanan adımlar tekrar yapılmaz.</div>`);
  if (d.possible_duplicate_of) {
    parts.push(`<div class="warn">Bu belge, daha önce yüklenen <a href="#/doc/${d.possible_duplicate_of}">başka bir belgeyle</a> aynı satıcı ve belge numarasına sahip. Mükerrer kayıt olabilir.</div>`);
  }
  if (d.status !== "processing" && d.field_counts.pending) {
    parts.push(`<div class="warn"><strong>${d.field_counts.pending} alan</strong> kontrol bekliyor. Sistem bu değerlerden emin değil ya da değerler birbiriyle tutarsız; onaylamadan önce belgedeki kaynağıyla karşılaştırın.</div>`);
  }
  $("#banner").innerHTML = parts.join("");
}

function allFields(d) {
  return [...d.header, ...d.rows.flatMap((r) => Object.values(r.cells))];
}

function renderPages(d) {
  const wrap = $("#pages");
  $("#page-info").textContent = d.pages.length ? `${d.pages.length} sayfa · ${d.source_kind === "pdf_text" ? "PDF metin katmanı" : "OCR"}` : "";
  wrap.innerHTML = d.pages.map((p) => `
    <div class="page" data-page="${p.page_no}" style="width:${zoom * 100}%">
      <img src="${withToken(`/api/documents/${d.id}/pages/${p.page_no}`)}" alt="Sayfa ${p.page_no}" loading="lazy">
    </div>`).join("");
  for (const f of allFields(d)) {
    if (!f.bbox || !f.page_no) continue;
    const page = wrap.querySelector(`.page[data-page="${f.page_no}"]`);
    if (!page) continue;
    const [x0, y0, x1, y1] = f.bbox;
    const box = document.createElement("div");
    box.className = `box ${f.status}`;
    box.dataset.fid = f.id;
    box.style.cssText = `left:${x0 * 100}%;top:${y0 * 100}%;width:${(x1 - x0) * 100}%;height:${(y1 - y0) * 100}%`;
    box.title = `${d.labels[f.name] || f.name}: ${f.raw_text ?? ""}`;
    box.onclick = () => focusField(f.id, false);
    page.appendChild(box);
  }
}

function setZoom(z) {
  zoom = Math.min(4, Math.max(0.5, z));
  document.querySelectorAll(".page").forEach((p) => (p.style.width = `${zoom * 100}%`));
}

function confBar(f) {
  const pct = Math.round((f.confidence || 0) * 100);
  return `<span class="conf" title="Güven">%${pct}<i><b style="width:${pct}%"></b></i></span>`;
}

function issueText(f) {
  return (f.issues || []).map((i) => ISSUES[i] || i);
}

function renderHeader(d) {
  $("#header-fields").innerHTML = d.header.map((f) => {
    const issues = issueText(f);
    const blocking = f.status === "needs_review";
    return `<div class="field ${f.status}" data-fid="${f.id}">
      <label for="in-${f.id}">${esc(d.labels[f.name] || f.name)}</label>
      <input id="in-${f.id}" data-fid="${f.id}" data-name="${f.name}" value="${esc(fmtValue(f.name, f.value))}" autocomplete="off">
      <div class="actions">
        ${blocking ? `<button class="link" data-confirm="${f.id}" title="Değer doğru">✓ Doğru</button>` : `<span class="badge ${f.status}">${STATUS[f.status]}</span>`}
      </div>
      <div class="meta">
        ${f.page_no ? `<button class="link small" data-show="${f.id}">Belgede göster</button>` : ""}
        ${f.raw_text ? `<span>OCR: “${esc(f.raw_text)}”</span>` : ""}
        ${f.value != null ? confBar(f) : ""}
        ${f.verified ? `<span class="verified" title="Başka alanlarla çapraz kontrol edildi">✓ doğrulandı</span>` : ""}
        ${issues.map((t) => `<span class="${blocking ? "issue" : ""}">${esc(t)}</span>`).join(" · ")}
        ${f.suggested_value ? `<button class="suggest" data-suggest="${f.id}" data-value="${esc(f.suggested_value)}">Öneri: ${esc(fmtValue(f.name, f.suggested_value))}</button>` : ""}
      </div>
    </div>`;
  }).join("");
  bindEditors($("#header-fields"));
}

function renderLines(d) {
  const cols = d.line_columns;
  const head = `<thead><tr><th>#</th>${cols.map((c) => `<th>${esc(d.labels[c])}</th>`).join("")}<th></th></tr></thead>`;
  const body = d.rows.map((r, i) => {
    const hasReview = Object.values(r.cells).some((c) => c.status === "needs_review");
    return `<tr class="${hasReview ? "has-review" : ""}"><td class="muted">${i + 1}</td>${cols.map((c) => {
      const f = r.cells[c];
      if (!f) return "<td></td>";
      const notes = f.status === "needs_review" ? issueText(f).join(", ") : "";
      return `<td class="${f.status} ${NUMERIC.has(c) ? "num" : "desc"}" data-fid="${f.id}">
        <input data-fid="${f.id}" data-name="${c}" value="${esc(fmtValue(c, f.value))}" title="${esc(`OCR: ${f.raw_text ?? "–"} · güven %${Math.round(f.confidence * 100)}${f.verified ? " · doğrulandı" : ""}`)}" autocomplete="off">
        ${notes ? `<div class="cell-note">${esc(notes)}${f.suggested_value ? ` <button class="suggest" data-suggest="${f.id}" data-value="${esc(f.suggested_value)}">${esc(fmtValue(c, f.suggested_value))}</button>` : ""}
          <button class="link small" data-confirm="${f.id}">✓</button></div>` : ""}
      </td>`;
    }).join("")}<td><button class="link danger" data-delrow="${r.row_index}" title="Satırı sil">✕</button></td></tr>`;
  }).join("");
  $("#lines").innerHTML = head + `<tbody>${body}</tbody>`;
  bindEditors($("#lines"));
}

function bindEditors(root) {
  root.querySelectorAll("input[data-fid]").forEach((input) => {
    input.dataset.orig = input.value;
    input.onfocus = () => highlight(input.dataset.fid, true);
    input.onkeydown = (e) => {
      if (e.key === "Enter") { e.preventDefault(); input.blur(); }
      if (e.key === "Escape") { input.value = input.dataset.orig; input.blur(); }
    };
    input.onchange = () => save(input.dataset.fid, input.value);
  });
  root.onclick = async (e) => {
    const t = e.target;
    if (t.dataset.confirm) {
      setDoc(await api(`/api/documents/${doc.id}/fields/${t.dataset.confirm}/confirm`, { method: "POST" }));
      refreshStats();
    } else if (t.dataset.suggest) {
      await save(t.dataset.suggest, t.dataset.value);
    } else if (t.dataset.show) {
      highlight(t.dataset.show, true);
    } else if (t.dataset.delrow !== undefined) {
      if (confirm("Satır silinsin mi?")) setDoc(await api(`/api/documents/${doc.id}/rows/${t.dataset.delrow}`, { method: "DELETE" }));
    }
  };
}

async function save(fid, value) {
  try {
    setDoc(await api(`/api/documents/${doc.id}/fields/${fid}`, { method: "PATCH", json: { value } }));
    toast("Kaydedildi; tutarlılık kontrolleri yeniden çalıştı");
    refreshStats();
    highlight(fid, false);
  } catch (err) {
    toast(err.message, 5000);
    renderHeader(doc);
    renderLines(doc);
  }
}

function highlight(fid, scroll) {
  activeId = fid;
  document.querySelectorAll(".box.active, .field.active, td.active").forEach((el) => el.classList.remove("active"));
  const box = document.querySelector(`.box[data-fid="${fid}"]`);
  document.querySelectorAll(`.field[data-fid="${fid}"], td[data-fid="${fid}"]`).forEach((el) => el.classList.add("active"));
  if (box) {
    box.classList.add("active");
    if (scroll) box.scrollIntoView({ behavior: "smooth", block: "center", inline: "center" });
  }
}

function focusField(fid, scrollDoc = true) {
  const input = document.querySelector(`input[data-fid="${fid}"]`);
  if (input) {
    input.scrollIntoView({ behavior: "smooth", block: "center" });
    input.focus({ preventScroll: true });
  }
  highlight(fid, scrollDoc);
}

function nextReview() {
  const pending = allFields(doc).filter((f) => f.status === "needs_review");
  if (!pending.length) return;
  const idx = pending.findIndex((f) => f.id === activeId);
  focusField(pending[(idx + 1) % pending.length].id);
}

async function loadHistory(id) {
  const rows = await api(`/api/documents/${id}/corrections`);
  $("#history").innerHTML = rows.length
    ? `<table><thead><tr><th>Zaman</th><th>Alan</th><th>İşlem</th><th>Önceki</th><th>Yeni</th><th>OCR okuması</th></tr></thead><tbody>${rows.map((r) => `
        <tr><td>${new Date(r.created_at * 1000).toLocaleTimeString("tr-TR")}</td><td>${esc(doc.labels[r.field_name] || r.field_name)}</td>
        <td>${ACTIONS[r.action] || r.action}</td><td>${esc(r.old_value ?? "–")}</td><td>${esc(r.new_value ?? "–")}</td><td class="muted">${esc(r.raw_text ?? "")}</td></tr>`).join("")}</tbody></table>`
    : `<p class="muted small">Henüz düzeltme yok.</p>`;
}

route();
