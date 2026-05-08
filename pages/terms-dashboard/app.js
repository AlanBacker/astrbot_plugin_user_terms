const bridge = window.AstrBotPluginPage;

const state = {
  payload: null,
  filter: "",
};

const els = {
  revision: document.getElementById("revision"),
  refresh: document.getElementById("refresh"),
  statAccepted: document.getElementById("statAccepted"),
  statPending: document.getElementById("statPending"),
  statRejected: document.getElementById("statRejected"),
  statOld: document.getElementById("statOld"),
  targets: document.getElementById("targets"),
  disabledTargets: document.getElementById("disabledTargets"),
  filter: document.getElementById("filter"),
  records: document.getElementById("records"),
  emptyRecords: document.getElementById("emptyRecords"),
  newTerms: document.getElementById("newTerms"),
  note: document.getElementById("note"),
  publish: document.getElementById("publish"),
  message: document.getElementById("message"),
};

const statusLabel = {
  accepted: "已同意",
  pending: "待签署",
  rejected: "已拒绝",
  pending_new_terms: "旧条款",
};

const scopeLabel = {
  user: "用户",
  group: "群组",
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatTime(value) {
  if (!value) return "-";
  return new Date(value * 1000).toLocaleString();
}

function setMessage(text, type = "") {
  els.message.textContent = text;
  els.message.className = type;
}

function renderStats(payload) {
  const stats = payload.stats || {};
  els.statAccepted.textContent = stats.accepted || 0;
  els.statPending.textContent = stats.pending || 0;
  els.statRejected.textContent = stats.rejected || 0;
  els.statOld.textContent = stats.pending_new_terms || 0;
}

function renderTargets(payload) {
  const targets = payload.configured_targets || [];
  els.targets.classList.toggle("empty", targets.length === 0);
  els.targets.innerHTML =
    targets
      .map((target) => {
        const stats = target.stats || {};
        const scope = escapeHtml(scopeLabel[target.scope_type] || target.scope_type);
        const sid = escapeHtml(target.scope_id);
        return `
          <div class="list-row">
            <div>
              <strong>${scope} ${sid}</strong>
              <span>${target.total_records || 0} 条记录</span>
            </div>
            <small>同意 ${stats.accepted || 0} / 待签 ${stats.pending || 0} / 拒绝 ${stats.rejected || 0}</small>
          </div>
        `;
      })
      .join("") || "暂无目标";

  const disabled = payload.disabled_targets || [];
  els.disabledTargets.classList.toggle("empty", disabled.length === 0);
  els.disabledTargets.innerHTML =
    disabled
      .map((target) => {
        const scope = escapeHtml(scopeLabel[target.scope_type] || target.scope_type);
        const sid = escapeHtml(target.scope_id);
        return `
          <div class="list-row disabled">
            <div>
              <strong>${scope} ${sid}</strong>
              <span>已禁用</span>
            </div>
          </div>
        `;
      })
      .join("") || "暂无禁用项";
}

function recordMatches(row) {
  if (!state.filter) return true;
  const haystack = [
    row.scope_type,
    row.scope_id,
    row.signer_user_id,
    row.signer_name,
    row.platform_id,
    row.group_id,
    row.message_origin,
  ]
    .join(" ")
    .toLowerCase();
  return haystack.includes(state.filter);
}

function renderRecords(payload) {
  const records = (payload.acceptances || []).filter(recordMatches);
  els.emptyRecords.hidden = records.length !== 0;
  els.records.innerHTML = records
    .map((row) => {
      const status = row.effective_status || row.status;
      return `
        <tr>
          <td>${escapeHtml(scopeLabel[row.scope_type] || row.scope_type)}</td>
          <td><code>${escapeHtml(row.scope_id)}</code></td>
          <td>
            <strong>${escapeHtml(row.signer_name || "-")}</strong>
            <span><code>${escapeHtml(row.signer_user_id)}</code></span>
          </td>
          <td><span class="badge ${escapeHtml(status)}">${escapeHtml(statusLabel[status] || status)}</span></td>
          <td>${escapeHtml(row.terms_revision)}</td>
          <td>${formatTime(row.last_seen_at)}</td>
        </tr>
      `;
    })
    .join("");
}

function render(payload) {
  state.payload = payload;
  els.revision.textContent = `当前条款版本：v${payload.current_revision}`;
  if (!els.newTerms.value.trim()) {
    els.newTerms.value = payload.active_terms_text || "";
  }
  renderStats(payload);
  renderTargets(payload);
  renderRecords(payload);
}

async function loadStatus() {
  setMessage("");
  els.refresh.disabled = true;
  try {
    const payload = await bridge.apiGet("status");
    render(payload);
  } catch (error) {
    setMessage(`读取失败：${error.message || error}`, "error");
  } finally {
    els.refresh.disabled = false;
  }
}

async function publishTerms() {
  const termsText = els.newTerms.value.trim();
  if (!termsText) {
    setMessage("请先填写新条款正文。", "error");
    return;
  }

  els.publish.disabled = true;
  setMessage("正在发布...");
  try {
    const result = await bridge.apiPost("publish", {
      terms_text: termsText,
      note: els.note.value.trim(),
    });
    setMessage(result.message || "已发布。", "success");
    await loadStatus();
  } catch (error) {
    setMessage(`发布失败：${error.message || error}`, "error");
  } finally {
    els.publish.disabled = false;
  }
}

await bridge.ready();
els.refresh.addEventListener("click", loadStatus);
els.publish.addEventListener("click", publishTerms);
els.filter.addEventListener("input", () => {
  state.filter = els.filter.value.trim().toLowerCase();
  if (state.payload) renderRecords(state.payload);
});
await loadStatus();
