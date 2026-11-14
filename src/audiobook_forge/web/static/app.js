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
  clearInterval(state.pollTimer);
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

    <div class="voice-mode">
      <label class="toggle" title="轻小说等难以判断说话人的文本，建议开启">
        <input type="checkbox" id="singleVoice"> 单声线（全书一个音色）
      </label>
      <span class="voice-pick" id="voicePick" hidden>
        <select id="singleVoiceSel"></select>
        <span class="muted small" id="voicePickHint">重新分段后生效</span>
      </span>
    </div>

    <div id="jobArea"></div>
    <div id="previewArea"></div>

    <div class="panel"><h3>角色与音色</h3>${castHtml}</div>

    <div class="panel"><h3>分片进度</h3>
      <p class="muted">总 ${seg.total || 0} 段 · 完成 ${seg.done || 0} · 失败 ${seg.failed || 0} · 待处理 ${(seg.pending || 0) + (seg.running || 0)}</p>
    </div>

    ${dl}

    <div class="panel">
      <h3>章节 <span class="muted" id="chSelInfo"></span></h3>
      <div class="chapter-tools">
        <button class="ghost small" id="selAll">全选</button>
        <button class="ghost small" id="selNone">清空</button>
        <button class="ghost small" id="selUndone">只选未合成</button>
        <button class="small primary" id="synthSel">合成选中章节</button>
      </div>
      <div class="scroll"><table>
        <thead><tr>
          <th style="width:30px"><input type="checkbox" id="selAllBox"></th>
          <th>#</th><th>标题</th><th>段落</th><th>对白</th><th>字数</th><th>已合成</th>
        </tr></thead>
        <tbody>${d.chapters
          .map((c) => `<tr>
            <td><input type="checkbox" class="chk" data-idx="${c.index}"></td>
            <td>${c.index + 1}</td>
            <td>${esc(c.title)}</td>
            <td>${c.blocks}</td>
            <td>${c.dialogues ? `<span class="pill dialogue">${c.dialogues}</span>` : "—"}</td>
            <td>${c.chars}</td>
            <td>${c.seg_total ? `<span class="pill ${c.seg_done >= c.seg_total ? "done" : "pending"}">${c.seg_done}/${c.seg_total}</span>` : "—"}</td>
          </tr>`)
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

  bindVoiceMode();

  bindChapterSelection(d);
  loadSegments();
}

function bindChapterSelection(detail) {
  const boxes = () => Array.from(document.querySelectorAll(".chk"));
  const selected = () => boxes().filter((b) => b.checked).map((b) => Number(b.dataset.idx));

  const info = $("#chSelInfo");
  const refresh = () => {
    const n = selected().length;
    if (info) info.textContent = n ? `已选 ${n} 章` : "未选章节（默认合成全书）";
    const all = $("#selAllBox");
    if (all) all.checked = n > 0 && n === boxes().length;
  };

  boxes().forEach((b) => (b.onchange = refresh));

  const allBox = $("#selAllBox");
  if (allBox) allBox.onchange = (e) => {
    boxes().forEach((b) => (b.checked = e.target.checked));
    refresh();
  };
  const on = (id, fn) => {
    const node = $(id);
    if (node) node.onclick = fn;
  };
  on("#selAll", () => { boxes().forEach((b) => (b.checked = true)); refresh(); });
  on("#selNone", () => { boxes().forEach((b) => (b.checked = false)); refresh(); });
  on("#selUndone", () => {
    (detail.chapters || []).forEach((c) => {
      const box = document.querySelector(`.chk[data-idx="${c.index}"]`);
      if (box) box.checked = !c.seg_total || c.seg_done < c.seg_total;
    });
    refresh();
  });
  on("#synthSel", () => {
    const chapters = selected();
    if (!chapters.length) {
      toast("先勾选要合成的章节", true);
      return;
    }
    startJob("synthesize", chapters);
  });

  refresh();
}

function formatDuration(ms) {
  const total = Math.round((ms || 0) / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h ? `${h} 小时 ${m} 分` : m ? `${m} 分 ${s} 秒` : `${s} 秒`;
}

/* ---------------------------------------------------------------- 任务 */

async function startJob(action, chapters) {
  const id = state.current;
  if (!id) return;
  const buttons = document.querySelectorAll(".actions button, .chapter-tools button");
  buttons.forEach((b) => (b.disabled = true));

  const singleBox = $("#singleVoice");
  const singleVoice = !!(singleBox && singleBox.checked);
  const voiceSel = $("#singleVoiceSel");
  const voice = singleVoice && voiceSel ? voiceSel.value : null;

  const payload = {};
  if (chapters && chapters.length) payload.chapters = chapters;
  if (singleVoice) {
    payload.single_voice = true;
    if (voice) payload.voice = voice;
  }

  try {
    const res = await api(`/api/books/${id}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const suffix =
      (chapters && chapters.length ? `（${chapters.length} 章）` : "") +
      (singleVoice ? "（单声线）" : "");
    toast(actionLabel(action) + suffix + " 已开始");
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
        <div class="progress-label">
          <span id="jobStage">排队中…</span>
          <span class="progress-right"><span id="jobCount"></span>
            <button class="ghost small" id="cancelJobBtn">中止</button></span>
        </div>
      </div>`;
    const btn = $("#cancelJobBtn");
    if (btn) {
      btn.onclick = async () => {
        btn.disabled = true;
        btn.textContent = "中止中…";
        try {
          await api(`/api/jobs/${jobId}/cancel`, { method: "POST" });
          toast("已发送中止请求，正在收尾");
        } catch (e) {
          toast(e.message, true);
          btn.disabled = false;
          btn.textContent = "中止";
        }
      };
    }
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
      const msg =
        job.status === "done"
          ? "任务完成"
          : job.status === "cancelled"
            ? "已中止（已完成的分片保留，可随时续跑）"
            : `任务结束：${job.message || job.status}`;
      toast(msg, job.status !== "done");
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

  $("#settingsBtn").onclick = openSettings;
  $("#settingsClose").onclick = closeSettings;
  $("#settingsCancel").onclick = closeSettings;
  $("#settingsSave").onclick = saveSettings;
  $("#settingsModal").onclick = (e) => {
    if (e.target.id === "settingsModal") closeSettings();
  };
  $("#ttsProvider").onchange = renderTtsState;
  $("#llmPreset").onchange = applyLlmPreset;

  loadStatus();
  loadBooks();
  setInterval(loadStatus, 20000);
}

/* ---------------------------------------------------------------- 设置 */

let settingsCache = null;

async function openSettings() {
  const modal = $("#settingsModal");
  if (!modal) return;
  modal.hidden = false;
  try {
    const s = await api("/api/settings");
    settingsCache = s;

    const ttsSel = $("#ttsProvider");
    ttsSel.innerHTML = s.tts.providers
      .map(
        (p) =>
          `<option value="${p.name}">${esc(p.label)}${p.ready ? "" : "（未配置）"}</option>`
      )
      .join("");
    ttsSel.value = s.tts.provider;

    const preset = $("#llmPreset");
    preset.innerHTML = s.presets
      .map((p) => `<option value="${p.id}">${esc(p.label)}</option>`)
      .join("");

    $("#llmEnabled").checked = s.llm.enabled;
    $("#llmBase").value = s.llm.base_url;
    $("#llmModel").value = s.llm.model;
    $("#llmKey").value = "";

    renderTtsState();
    renderLlmState();
  } catch (e) {
    toast(e.message, true);
  }
}

function closeSettings() {
  const modal = $("#settingsModal");
  if (modal) modal.hidden = true;
}

function renderTtsState() {
  if (!settingsCache) return;
  const name = $("#ttsProvider").value;
  const p = settingsCache.tts.providers.find((x) => x.name === name);
  const el = $("#ttsState");
  if (el) el.textContent = p ? `${p.label}：${p.ready ? "可用" : "未就绪"} · ${p.message}` : "";
}

function renderLlmState() {
  const el = $("#llmState");
  if (!settingsCache || !el) return;
  const s = settingsCache.llm;
  el.textContent = [
    s.enabled ? "已启用" : "已关闭（用规则兜底）",
    s.usable ? "地址与密钥齐全" : "地址或密钥不完整",
    s.has_key ? "已保存密钥" : "尚未保存密钥",
  ].join(" · ");
}

function applyLlmPreset() {
  if (!settingsCache) return;
  const p = settingsCache.presets.find((x) => x.id === $("#llmPreset").value);
  if (!p) return;
  if (p.base_url) $("#llmBase").value = p.base_url;
  if (p.model) $("#llmModel").value = p.model;
}

async function saveSettings() {
  const btn = $("#settingsSave");
  btn.disabled = true;
  try {
    const s = await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tts_provider: $("#ttsProvider").value,
        llm_enabled: $("#llmEnabled").checked,
        llm_base_url: $("#llmBase").value.trim(),
        llm_model: $("#llmModel").value.trim(),
        llm_api_key: $("#llmKey").value.trim(),
      }),
    });
    settingsCache = s;
    renderTtsState();
    renderLlmState();
    closeSettings();
    toast("设置已保存，下一个任务生效");
    await loadStatus();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

/* ---------------------------------------------------------------- 单声线 */

const VOICE_MODE_KEY = "abf.singleVoice";
const VOICE_NAME_KEY = "abf.singleVoiceName";

async function bindVoiceMode() {
  const box = $("#singleVoice");
  const pick = $("#voicePick");
  const sel = $("#singleVoiceSel");
  if (!box || !pick || !sel) return;

  box.checked = localStorage.getItem(VOICE_MODE_KEY) === "1";
  pick.hidden = !box.checked;

  box.onchange = () => {
    localStorage.setItem(VOICE_MODE_KEY, box.checked ? "1" : "0");
    pick.hidden = !box.checked;
    if (box.checked) loadVoices();
  };

  if (box.checked) loadVoices();
}

async function loadVoices() {
  const sel = $("#singleVoiceSel");
  if (!sel || sel.dataset.loaded === "1") return;
  try {
    const data = await api("/api/voices");
    const voices = data.voices || [];
    sel.innerHTML = voices.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
    const saved = localStorage.getItem(VOICE_NAME_KEY);
    if (saved && voices.includes(saved)) sel.value = saved;
    sel.dataset.loaded = "1";
    sel.onchange = () => localStorage.setItem(VOICE_NAME_KEY, sel.value);
  } catch (e) {
    toast(e.message, true);
  }
}

document.addEventListener("DOMContentLoaded", init);
