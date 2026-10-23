const bridge = window.AstrBotPluginPage;

const state = {
  payload: null,
  filter: "",
  statusFilter: "",
  selected: new Map(),
};

const els = {
  revision: document.getElementById("revision"),
  scopeModes: document.getElementById("scopeModes"),
  refresh: document.getElementById("refresh"),
  statAccepted: document.getElementById("statAccepted"),
  statPending: document.getElementById("statPending"),
  statRejected: document.getElementById("statRejected"),
  statOld: document.getElementById("statOld"),
  targets: document.getElementById("targets"),
  exemptTargets: document.getElementById("exemptTargets"),
  disabledTargets: document.getElementById("disabledTargets"),
  filter: document.getElementById("filter"),
  statusFilter: document.getElementById("statusFilter"),
  selectedCount: document.getElementById("selectedCount"),
  bulkStatus: document.getElementById("bulkStatus"),
  applySelected: document.getElementById("applySelected"),
  clearSelected: document.getElementById("clearSelected"),
  bulkMessage: document.getElementById("bulkMessage"),
  selectAll: document.getElementById("selectAll"),
  records: document.getElementById("records"),
  emptyRecords: document.getElementById("emptyRecords"),
  newTerms: document.getElementById("newTerms"),
  note: document.getElementById("note"),
  publish: document.getElementById("publish"),
  message: document.getElementById("message"),
  batchScope: document.getElementById("batchScope"),
  batchStatus: document.getElementById("batchStatus"),
  batchSids: document.getElementById("batchSids"),
  batchApply: document.getElementById("batchApply"),
  batchMessage: document.getElementById("batchMessage"),
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

const sourceLabel = {
  manual: "手动",
  auto: "自动",
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

function setMessage(text, type = "", el = els.message) {
  el.textContent = text;
  el.className = type;
}

function targetKey(scopeType, scopeId) {
  return JSON.stringify([scopeType, scopeId]);
}

function renderStats(payload) {
  const stats = payload.stats || {};
  els.statAccepted.textContent = stats.accepted || 0;
  els.statPending.textContent = stats.pending || 0;
  els.statRejected.textContent = stats.rejected || 0;
  els.statOld.textContent = stats.pending_new_terms || 0;
}

function renderScopeModes(payload) {
  const modes = payload.scope_modes || {};
  const user = (modes.user && modes.user.label) || "-";
  const group = (modes.group && modes.group.label) || "-";
  els.scopeModes.textContent = `生效范围：用户 ${user} · 群组 ${group}`;
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
        const source = target.source === "auto" ? "auto" : "manual";
        return `
          <div class="list-row${target.disabled ? " disabled" : ""}">
            <div>
              <strong>${scope} ${sid}<span class="tag ${source}">${sourceLabel[source]}</span></strong>
              <span>${target.total_records || 0} 条记录</span>
            </div>
            <small>同意 ${stats.accepted || 0} / 待签 ${stats.pending || 0} / 拒绝 ${stats.rejected || 0}</small>
          </div>
        `;
      })
      .join("") || "暂无目标";

  const exempt = payload.exempt_targets || [];
  els.exemptTargets.classList.toggle("empty", exempt.length === 0);
  els.exemptTargets.innerHTML =
    exempt
      .map((target) => {
        const scope = escapeHtml(scopeLabel[target.scope_type] || target.scope_type);
        const sid = escapeHtml(target.scope_id);
        return `
          <div class="list-row exempt">
            <div>
              <strong>${scope} ${sid}</strong>
              <span>无需签署</span>
            </div>
          </div>
        `;
      })
      .join("") || "暂无豁免项";

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
  if (state.statusFilter && (row.effective_status || row.status) !== state.statusFilter) {
    return false;
  }
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

function visibleRecords(payload) {
  return (payload.acceptances || []).filter(recordMatches);
}

function renderSelection() {
  const count = state.selected.size;
  els.selectedCount.textContent = `已选 ${count} 个目标`;
  els.applySelected.disabled = count === 0;
  els.clearSelected.disabled = count === 0;

  const keys = state.payload
    ? visibleRecords(state.payload).map((row) => targetKey(row.scope_type, row.scope_id))
    : [];
  const checkedCount = keys.filter((key) => state.selected.has(key)).length;
  els.selectAll.disabled = keys.length === 0;
  els.selectAll.checked = keys.length > 0 && checkedCount === keys.length;
  els.selectAll.indeterminate = checkedCount > 0 && checkedCount < keys.length;
}

function renderRecords(payload) {
  const records = visibleRecords(payload);
  els.emptyRecords.hidden = records.length !== 0;
  els.records.innerHTML = records
    .map((row) => {
      const status = row.effective_status || row.status;
      const key = targetKey(row.scope_type, row.scope_id);
      return `
        <tr${state.selected.has(key) ? ' class="selected"' : ""}>
          <td class="check">
            <input type="checkbox" data-key="${escapeHtml(key)}"${state.selected.has(key) ? " checked" : ""} aria-label="选择 ${escapeHtml(row.scope_id)}" />
          </td>
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
  renderSelection();
}

function pruneSelection(payload) {
  const known = new Set(
    (payload.acceptances || []).map((row) => targetKey(row.scope_type, row.scope_id)),
  );
  for (const key of state.selected.keys()) {
    if (!known.has(key)) state.selected.delete(key);
  }
}

function render(payload) {
  state.payload = payload;
  els.revision.textContent = `当前条款版本：v${payload.current_revision}`;
  if (!els.newTerms.value.trim()) {
    els.newTerms.value = payload.active_terms_text || "";
  }
  pruneSelection(payload);
  renderScopeModes(payload);
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

async function batchUpdate(targets, status, button, messageEl) {
  button.disabled = true;
  setMessage("正在修改...", "", messageEl);
  try {
    const result = await bridge.apiPost("batch_status", { status, targets });
    setMessage(result.message || "已修改。", "success", messageEl);
    await loadStatus();
    return true;
  } catch (error) {
    setMessage(`修改失败：${error.message || error}`, "error", messageEl);
    return false;
  } finally {
    button.disabled = false;
  }
}

async function applySelected() {
  const targets = [...state.selected.values()];
  if (targets.length === 0) return;
  const ok = await batchUpdate(targets, els.bulkStatus.value, els.applySelected, els.bulkMessage);
  if (ok) {
    state.selected.clear();
  }
  renderSelection();
  if (state.payload) renderRecords(state.payload);
}

async function applySidList() {
  const scopeType = els.batchScope.value;
  const sids = [
    ...new Set(
      els.batchSids.value
        .split(/[\s,;，；、]+/)
        .map((sid) => sid.trim())
        .filter(Boolean),
    ),
  ];
  if (sids.length === 0) {
    setMessage("请先填写至少一个 SID。", "error", els.batchMessage);
    return;
  }
  const targets = sids.map((sid) => ({ scope_type: scopeType, scope_id: sid }));
  const ok = await batchUpdate(targets, els.batchStatus.value, els.batchApply, els.batchMessage);
  if (ok) {
    els.batchSids.value = "";
  }
}

function toggleRecord(event) {
  const input = event.target;
  if (!input.matches("input[data-key]")) return;
  const key = input.dataset.key;
  if (input.checked) {
    const [scopeType, scopeId] = JSON.parse(key);
    state.selected.set(key, { scope_type: scopeType, scope_id: scopeId });
  } else {
    state.selected.delete(key);
  }
  if (state.payload) renderRecords(state.payload);
}

function toggleAllVisible() {
  if (!state.payload) return;
  const checked = els.selectAll.checked;
  for (const row of visibleRecords(state.payload)) {
    const key = targetKey(row.scope_type, row.scope_id);
    if (checked) {
      state.selected.set(key, { scope_type: row.scope_type, scope_id: row.scope_id });
    } else {
      state.selected.delete(key);
    }
  }
  renderRecords(state.payload);
}

await bridge.ready();
els.refresh.addEventListener("click", loadStatus);
els.publish.addEventListener("click", publishTerms);
els.applySelected.addEventListener("click", applySelected);
els.clearSelected.addEventListener("click", () => {
  state.selected.clear();
  setMessage("", "", els.bulkMessage);
  if (state.payload) renderRecords(state.payload);
});
els.batchApply.addEventListener("click", applySidList);
els.records.addEventListener("change", toggleRecord);
els.selectAll.addEventListener("change", toggleAllVisible);
els.filter.addEventListener("input", () => {
  state.filter = els.filter.value.trim().toLowerCase();
  if (state.payload) renderRecords(state.payload);
});
els.statusFilter.addEventListener("change", () => {
  state.statusFilter = els.statusFilter.value;
  if (state.payload) renderRecords(state.payload);
});
await loadStatus();
