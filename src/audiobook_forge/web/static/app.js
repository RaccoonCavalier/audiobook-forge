const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, html) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (html !== undefined) node.innerHTML = html;
  return node;
};
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = { books: [], current: null, detail: null, pollTimer: null, segOffset: 0 };

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const type = res.headers.get("content-type") || "";
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const data = await res.json();
      msg = data.detail || msg;
    } catch (_) { /* 非 JSON */ }
    throw new Error(msg);
  }
  return type.includes("application/json") ? res.json() : res;
}

function toast(message, isError = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = "toast show" + (isError ? " err" : "");
  clearTimeout(node._timer);
  node._timer = setTimeout(() => (node.className = "toast"), 3600);
}

/* ---------------------------------------------------------------- 状态栏 */

async function loadStatus() {
  try {
    const s = await api("/api/status");
    const p = s.providers[s.provider] || {};
    const dot = p.ready ? "ok" : "bad";
    $("#status").innerHTML =
      `<span class="dot ${dot}"></span> TTS：${esc(p.label || s.provider)}` +
      `<span class="dot ${s.llm.ready ? "ok" : "warn"}"></span> LLM：${esc(s.llm.model)}` +
      `<span class="fab-badge">v${esc(s.version)}</span>`;
  } catch (e) {
    $("#status").innerHTML = `<span class="dot bad"></span> 服务未就绪`;
  }
}

/* ---------------------------------------------------------------- 书架 */

async function loadBooks() {
  try {
    state.books = await api("/api/books");
  } catch (e) {
    state.books = [];
  }
  renderShelf();
}

function renderShelf() {
  const shelf = $("#shelf");
  shelf.innerHTML = "";
  if (!state.books.length) {
    shelf.appendChild(el("li", "", `<span class="muted">书架是空的</span>`));
    return;
  }
  for (const book of state.books) {
    const li = el("li");
    li.dataset.id = book.id;
    if (state.current === book.id) li.classList.add("active");
    const job = book.job;
    const pct = job && job.total ? Math.round((job.done / job.total) * 100) : null;
    li.innerHTML = `
      <span class="title">${esc(book.title || "未命名")}</span>
      <span class="meta">
        <span>${book.chapters} 章</span>
        <span>${Math.round((book.chars || 0) / 1000)}k 字</span>
        ${book.has_output ? '<span style="color:var(--ok)">已导出</span>' : ""}
        ${pct !== null && job.status === "running" ? `<span style="color:var(--accent)">${pct}%</span>` : ""}
      </span>`;
    li.onclick = () => openBook(book.id);
    shelf.appendChild(li);
  }
}

/* ---------------------------------------------------------------- 详情 */

async function openBook(id) {
  state.current = id;
  state.segOffset = 0;
  renderShelf();
  $("#detail").innerHTML = `<p class="muted">加载中…</p>`;
  try {
    state.detail = await api(`/api/books/${id}`);
    renderDetail();
    if (state.detail.job && state.detail.job.status === "running") {
      pollJob(state.detail.job.id);
    }
  } catch (e) {
    $("#detail").innerHTML = `<p class="muted">加载失败：${esc(e.message)}</p>`;
  }
}

function renderDetail() {
  const d = state.detail;
  const b = d.book;
  const seg = d.segments || {};

  const castHtml = d.cast
    ? `<div class="cast-grid">
         <div class="cast-chip"><b>旁白</b><small>${esc(d.cast.narrator_voice)}</small></div>
         ${(d.cast.assignments || [])
           .map((a) => `<div class="cast-chip"><b>${esc(a.speaker)}</b><small>${esc(a.voice)}</small></div>`)
           .join("")}
       </div>`
    : `<p class="muted">还没有角色表，先跑一次「分析」。</p>`;

  const manifest = d.manifest;
  const dl = manifest
    ? `<div class="panel"><h3>成品</h3>
         <p><a class="dl" href="/api/books/${b.id}/download?fmt=mp3">⬇ MP3</a>
         ${manifest.m4b ? ` · <a class="dl" href="/api/books/${b.id}/download?fmt=m4b">⬇ M4B（带章节）</a>` : ""}
         <span class="muted"> · ${formatDuration(manifest.duration_ms)} · ${manifest.segment_count} 段</span></p>
       </div>`
    : "";

  $("#detail").innerHTML = `
    <div class="book-head">
      <div>
        <h2>${esc(b.title)}</h2>
        <div class="book-meta">
          <span>${esc(b.author || "佚名")}</span>
          <span>${esc(b.format.toUpperCase())}</span>
          <span>${b.chapters} 章</span>
          <span>${b.chars} 字</span>
          <span>${b.blocks} 段落</span>
        </div>
      </div>
      <div>
        <button class="ghost small" id="deleteBtn">删除</button>
      </div>
    </div>

    <div class="actions">
      <button class="primary" id="runBtn">🎬 一键生成</button>
      <button id="analyzeBtn">🧠 分析</button>
      <button id="synthBtn">🔊 合成</button>
      <button id="assembleBtn">📦 导出</button>
      <button class="ghost" id="previewBtn">🎧 试听音色</button>
    </div>

    <div id="jobArea"></div>
    <div id="previewArea"></div>

    <div class="panel"><h3>角色与音色</h3>${castHtml}</div>

    <div class="panel"><h3>分片进度</h3>
      <p class="muted">总 ${seg.total || 0} 段 · 完成 ${seg.done || 0} · 失败 ${seg.failed || 0} · 待处理 ${(seg.pending || 0) + (seg.running || 0)}</p>
    </div>

    ${dl}

    <div class="panel">
      <h3>章节</h3>
      <div class="scroll"><table>
        <thead><tr><th>#</th><th>标题</th><th>段落</th><th>对白</th><th>字数</th></tr></thead>
        <tbody>${d.chapters
          .map((c) => `<tr><td>${c.index + 1}</td><td>${esc(c.title)}</td><td>${c.blocks}</td>
             <td>${c.dialogues ? `<span class="pill dialogue">${c.dialogues}</span>` : "—"}</td><td>${c.chars}</td></tr>`)
          .join("")}</tbody>
      </table></div>
    </div>

    <div class="panel"><h3>分片明细</h3><div id="segments"></div></div>
  `;

  $("#deleteBtn").onclick = () => deleteBook(b.id);
  $("#runBtn").onclick = () => startJob("run");
  $("#analyzeBtn").onclick = () => startJob("analyze");
  $("#synthBtn").onclick = () => startJob("synthesize");
  $("#assembleBtn").onclick = () => startJob("assemble");
  $("#previewBtn").onclick = openPreview;

  loadSegments();
}

function formatDuration(ms) {
  const total = Math.round((ms || 0) / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h ? `${h} 小时 ${m} 分` : m ? `${m} 分 ${s} 秒` : `${s} 秒`;
}

/* ---------------------------------------------------------------- 任务 */

async function startJob(action) {
  const id = state.current;
  if (!id) return;
  const buttons = document.querySelectorAll(".actions button");
  buttons.forEach((b) => (b.disabled = true));
  try {
    const res = await api(`/api/books/${id}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    toast(actionLabel(action) + " 已开始");
    pollJob(res.job_id);
  } catch (e) {
    toast(e.message, true);
    buttons.forEach((b) => (b.disabled = false));
  }
}

function actionLabel(action) {
  return { run: "一键生成", analyze: "分析", synthesize: "合成", assemble: "导出" }[action] || action;
}

function pollJob(jobId) {
  clearInterval(state.pollTimer);
  const area = $("#jobArea");
  if (area) {
    area.innerHTML = `
      <div class="progress-wrap">
        <div class="progress"><i id="bar"></i></div>
        <div class="progress-label"><span id="jobStage">排队中…</span><span id="jobCount"></span></div>
      </div>`;
  }

  state.pollTimer = setInterval(async () => {
    let job;
    try {
      job = await api(`/api/jobs/${jobId}`);
    } catch (e) {
      clearInterval(state.pollTimer);
      return;
    }
    const bar = $("#bar");
    const pct = job.total ? Math.min(100, (job.done / job.total) * 100) : job.status === "running" ? 5 : 0;
    if (bar) bar.style.width = `${pct}%`;
    const stage = $("#jobStage");
    if (stage) stage.textContent = `${job.stage} · ${job.message || ""}`;
    const count = $("#jobCount");
    if (count) count.textContent = job.total ? `${job.done} / ${job.total}` : "";

    if (["done", "failed", "cancelled"].includes(job.status)) {
      clearInterval(state.pollTimer);
      toast(job.status === "done" ? "任务完成" : `任务结束：${job.message || job.status}`, job.status !== "done");
      await loadBooks();
      await openBook(state.current);
    }
  }, 900);
}

/* ---------------------------------------------------------------- 分片 */

async function loadSegments() {
  const box = $("#segments");
  if (!box) return;
  try {
    const data = await api(`/api/books/${state.current}/segments?offset=${state.segOffset}&limit=50`);
    const rows = data.items
      .map((s) => {
        const playable = s.status === "done";
        return `<tr>
          <td>${s.seq}</td>
          <td>${s.speaker ? `<span class="speaker">${esc(s.speaker)}</span><br><small class="muted">${esc(s.voice)}</small>` : `<small class="muted">${esc(s.voice)}</small>`}</td>
          <td class="text-cell">${s.style ? `<span class="pill">${esc(s.style)}</span> ` : ""}${esc(s.text.slice(0, 90))}${s.text.length > 90 ? "…" : ""}</td>
          <td><span class="pill ${s.status}">${s.status}</span></td>
          <td>${playable ? `<button class="ghost small" onclick="playSeg(${s.seq})">▶</button>` : "—"}</td>
        </tr>`;
      })
      .join("");

    box.innerHTML = `
      <div class="scroll"><table>
        <thead><tr><th>#</th><th>说话人 / 音色</th><th>文本</th><th>状态</th><th></th></tr></thead>
        <tbody>${rows || '<tr><td colspan="5" class="muted">还没有分片</td></tr>'}</tbody>
      </table></div>
      <div class="pager">
        <button class="ghost small" id="prevPage" ${state.segOffset === 0 ? "disabled" : ""}>← 上一页</button>
        <span>${state.segOffset + 1} – ${Math.min(state.segOffset + 50, data.total)} / ${data.total}</span>
        <button class="ghost small" id="nextPage" ${state.segOffset + 50 >= data.total ? "disabled" : ""}>下一页 →</button>
      </div>`;

    $("#prevPage").onclick = () => {
      state.segOffset = Math.max(0, state.segOffset - 50);
      loadSegments();
    };
    $("#nextPage").onclick = () => {
      state.segOffset += 50;
      loadSegments();
    };
  } catch (e) {
    box.innerHTML = `<p class="muted">${esc(e.message)}</p>`;
  }
}

let audioEl = null;
function playSeg(seq) {
  if (audioEl) audioEl.pause();
  audioEl = new Audio(`/api/books/${state.current}/audio/${seq}`);
  audioEl.play().catch(() => toast("无法播放该分片", true));
}
window.playSeg = playSeg;

/* ---------------------------------------------------------------- 试听 */

async function openPreview() {
  const area = $("#previewArea");
  let info;
  try {
    info = await api("/api/voices");
  } catch (e) {
    toast(e.message, true);
    return;
  }
  area.innerHTML = `
    <div class="panel">
      <h3>试听音色 · ${esc(info.label || info.provider)} ${info.ready ? "" : "（未就绪）"}</h3>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <select id="pvVoice" style="padding:8px;border-radius:8px;background:var(--panel-2);color:var(--text);border:1px solid var(--line)">
          ${(info.voices || []).map((v) => `<option>${esc(v)}</option>`).join("")}
        </select>
        <input id="pvText" value="夜色渐深，远处的灯火像碎金撒在江面上。" style="flex:1;min-width:240px;padding:8px;border-radius:8px;background:var(--panel-2);color:var(--text);border:1px solid var(--line)">
        <button class="primary small" id="pvGo">合成并播放</button>
      </div>
    </div>`;
  $("#pvGo").onclick = async () => {
    const voice = $("#pvVoice").value;
    const text = $("#pvText").value.trim();
    if (!text) return;
    $("#pvGo").disabled = true;
    $("#pvGo").textContent = "合成中…";
    try {
      const res = await fetch("/api/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, voice }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || "合成失败");
      }
      const blob = await res.blob();
      new Audio(URL.createObjectURL(blob)).play();
    } catch (e) {
      toast(e.message, true);
    } finally {
      $("#pvGo").disabled = false;
      $("#pvGo").textContent = "合成并播放";
    }
  };
}

/* ---------------------------------------------------------------- 上传 */

async function uploadFile(file) {
  if (!file) return;
  const dz = $("#dropzone");
  dz.classList.add("drag");
  toast(`正在解析 ${file.name}…`);
  const form = new FormData();
  form.append("file", file);
  try {
    const res = await api("/api/books", { method: "POST", body: form });
    toast(`已导入《${res.title}》：${res.chapters} 章 / ${res.chars} 字`);
    await loadBooks();
    await openBook(res.book_id);
  } catch (e) {
    toast(e.message, true);
  } finally {
    dz.classList.remove("drag");
  }
}

async function deleteBook(id) {
  if (!confirm("删除这本书及其所有音频？")) return;
  try {
    await api(`/api/books/${id}`, { method: "DELETE" });
    state.current = null;
    state.detail = null;
    $("#detail").innerHTML = `<div class="empty"><div class="empty-icon">📚</div><h2>还没有选中书</h2><p class="muted">上传一本书，或从左侧书架里挑一本</p></div>`;
    await loadBooks();
    toast("已删除");
  } catch (e) {
    toast(e.message, true);
  }
}

/* ---------------------------------------------------------------- 初始化 */

function init() {
  const dz = $("#dropzone");
  const input = $("#fileInput");
  dz.onclick = () => input.click();
  input.onchange = () => {
    uploadFile(input.files[0]);
    input.value = "";
  };
  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      dz.classList.add("drag");
    })
  );
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      dz.classList.remove("drag");
    })
  );
  dz.addEventListener("drop", (e) => uploadFile(e.dataTransfer.files[0]));
  $("#refreshBtn").onclick = async () => {
    await loadBooks();
    toast("已刷新");
  };

  loadStatus();
  loadBooks();
  setInterval(loadStatus, 20000);
}

document.addEventListener("DOMContentLoaded", init);
