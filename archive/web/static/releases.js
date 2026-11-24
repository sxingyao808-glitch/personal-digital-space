/* Release management UI (P4).
 *
 * Self-contained owner page: history, release detail, read-only preview,
 * publish, withdraw and historical restore. Every write is confirmed; no
 * non-idempotent request is retried automatically when the outcome is unclear.
 */
(() => {
  "use strict";

  const STATUS_LABELS = {
    active: "已发布到本机",
    prepared: "已准备未公开",
    superseded: "已被替代",
    withdrawn: "已撤回",
  };

  const ACTION_LABELS = {
    publish: "发布",
    prepared: "准备",
    withdraw_all: "整体撤回",
    withdraw_subset: "部分撤回",
    restore_full: "全量恢复",
    restore_subset: "部分恢复",
    build: "构建",
    activate: "激活",
    supersede: "替代",
    withdraw: "撤回",
    restore_request: "恢复请求",
  };

  const HISTORY_LIMIT = 60;

  const DRIFT_LABELS = {
    PUBLISHED_AND_CURRENT: "公开副本与当前一致",
    PUBLISHED_SNAPSHOT_STALE: "公开快照已过期",
    PUBLISHED_BUT_NOW_PRIVATE: "已公开但仍在线 · 当前已改为私有",
    PUBLISHED_BUT_DELETED: "已公开但仍在线 · 当前已删除",
    PUBLISHED_BUT_DESELECTED: "已公开但仍在线 · 已取消选择公开",
    PUBLISHED_ATTACHMENT_DRIFT: "附件与已发布副本不一致",
    SELECTED_BUT_NOT_PUBLISHED: "已选择公开但未被当前版本收录",
    NOT_IN_ACTIVE_RELEASE: "尚未进入任何公开版本",
  };

  const DRIFT_SEVERITY = {
    PUBLISHED_AND_CURRENT: "ok",
    PUBLISHED_SNAPSHOT_STALE: "warn",
    PUBLISHED_BUT_NOW_PRIVATE: "risk",
    PUBLISHED_BUT_DELETED: "risk",
    PUBLISHED_BUT_DESELECTED: "risk",
    PUBLISHED_ATTACHMENT_DRIFT: "risk",
    SELECTED_BUT_NOT_PUBLISHED: "warn",
    NOT_IN_ACTIVE_RELEASE: "info",
  };

  const state = {
    csrf: "",
    releases: [],
    activeId: null,
    activeSince: null,
    revision: null,
    activeManifest: null,
    drift: null,
    history: [],
    cursor: null,
    historyTotal: 0,
    historyLoaded: false,
    contents: [],
    contentDetails: new Map(),
    publishSelected: new Set(),
    publishAttachments: new Set(),
    previewValid: false,
    publishPlan: null,
    publication: null,
    withdrawSelected: new Set(),
    restoreSourceId: "",
    restoreManifest: null,
    restoreRecord: null,
    restoreSelected: new Set(),
    restoreScope: "full",
    dryRun: null,
    expectedActiveId: null,
    restoreExpectedActiveId: null,
    detailId: null,
    busy: false,
    loaded: false,
  };

  // ------------------------------------------------------------------
  // Small utilities
  // ------------------------------------------------------------------
  function byId(id) {
    return document.getElementById(id);
  }

  function setText(node, value) {
    if (!node) return;
    node.textContent = value === null || value === undefined ? "" : String(value);
  }

  function clearNode(node) {
    if (!node) return;
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function fmtTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    try {
      return date.toLocaleString("zh-CN", { hour12: false });
    } catch (err) {
      return date.toISOString();
    }
  }

  function fmtBytes(value) {
    const size = Number(value);
    if (!Number.isFinite(size) || size < 0) return "—";
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  }

  function localInputToIso(value) {
    if (!value) return null;
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return null;
    return date.toISOString();
  }

  function shortId(value) {
    if (!value) return "—";
    const text = String(value);
    if (text.length <= 24) return text;
    return `${text.slice(0, 15)}…${text.slice(-6)}`;
  }

  function plural(count, noun) {
    return `${count} 个${noun}`;
  }

  function statusLabel(status) {
    return STATUS_LABELS[status] || String(status || "未知");
  }

  function actionLabel(action) {
    return ACTION_LABELS[action] || String(action || "未知");
  }

  function operationOf(record) {
    return record.operation || actionLabelFromRecord(record);
  }

  function actionLabelFromRecord(record) {
    if (record.action === "restore") {
      return record.restore_scope === "full" ? "restore_full" : "restore_subset";
    }
    if (record.withdrawal_scope === "all") return "withdraw_all";
    if (record.withdrawal_scope === "subset") return "withdraw_subset";
    if (record.activated_at) return "publish";
    return "prepared";
  }

  function showToast(message, tone) {
    const toast = byId("release-toast");
    if (!toast) return;
    setText(toast, message);
    toast.className = `toast${tone ? ` ${tone}` : ""}`;
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => {
      toast.classList.add("hidden");
    }, 3600);
  }

  function setBusy(busy) {
    state.busy = busy;
    const refresh = byId("release-refresh");
    if (refresh) refresh.disabled = busy;
    syncPublishButtons();
    syncWithdrawButton();
    syncRestoreButtons();
    syncOneClickButton();
  }

  // ------------------------------------------------------------------
  // API
  // ------------------------------------------------------------------
  async function api(method, path, body) {
    const options = {
      method,
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    };
    if (method !== "GET") {
      options.headers["X-CSRF-Token"] = state.csrf;
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body || {});
    }
    let response;
    try {
      response = await fetch(path, options);
    } catch (err) {
      return {
        ok: false,
        uncertain: true,
        error: {
          code: "network_error",
          message: "网络连接失败，操作结果不确定。",
        },
      };
    }
    if (response.status === 401) {
      window.location.assign("/login");
      return { ok: false, error: { code: "unauthorized", message: "需要所有者身份。" } };
    }
    let payload = null;
    try {
      payload = await response.json();
    } catch (err) {
      payload = null;
    }
    if (payload && payload.ok === true) {
      return { ok: true, status: response.status, data: payload.data };
    }
    return {
      ok: false,
      status: response.status,
      uncertain: response.status >= 500,
      error:
        (payload && payload.error) || {
          code: "invalid_response",
          message: "服务返回了无法识别的响应。",
        },
    };
  }

  function confirmDialog({ title, message, confirmLabel }) {
    return new Promise((resolve) => {
      const dialog = byId("release-confirm-dialog");
      const okButton = byId("release-confirm-ok");
      const cancelButton = byId("release-confirm-cancel");
      if (!dialog || !okButton || !cancelButton) {
        resolve(window.confirm(message || title || "确认执行该操作？"));
        return;
      }
      setText(byId("release-confirm-title"), title || "确认");
      setText(byId("release-confirm-message"), message || "");
      setText(okButton, confirmLabel || "确认");

      let settled = false;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        okButton.removeEventListener("click", onOk);
        cancelButton.removeEventListener("click", onCancel);
        dialog.removeEventListener("cancel", onCancel);
        if (dialog.open) dialog.close();
        resolve(value);
      };
      const onOk = () => finish(true);
      const onCancel = (event) => {
        if (event && event.type === "cancel") event.preventDefault();
        finish(false);
      };
      okButton.addEventListener("click", onOk);
      cancelButton.addEventListener("click", onCancel);
      dialog.addEventListener("cancel", onCancel);
      if (typeof dialog.showModal === "function") {
        dialog.showModal();
      } else {
        dialog.setAttribute("open", "open");
      }
    });
  }

  // ------------------------------------------------------------------
  // Result rendering
  // ------------------------------------------------------------------
  function resetResult(node) {
    if (!node) return;
    clearNode(node);
    node.className = "release-result hidden";
  }

  function renderResult(node, { tone, title, lines, actions }) {
    if (!node) return;
    clearNode(node);
    node.className = `release-result ${tone || ""}`.trim();
    if (title) {
      const heading = el("p", "release-result-title", title);
      node.appendChild(heading);
    }
    if (lines && lines.length) {
      const list = el("ul", "release-result-list");
      lines.forEach((line) => {
        if (line) list.appendChild(el("li", null, line));
      });
      node.appendChild(list);
    }
    if (actions && actions.length) {
      const row = el("div", "release-result-actions");
      actions.forEach((action) => {
        const button = el("button", "ghost-button", action.label);
        button.type = "button";
        button.addEventListener("click", action.onClick);
        row.appendChild(button);
      });
      node.appendChild(row);
    }
  }

  function issueLines(issues) {
    if (!Array.isArray(issues)) return [];
    return issues.map((issue) => {
      if (!issue || typeof issue !== "object") return String(issue);
      const target = issue.target ? `${issue.target}：` : "";
      const detail = issue.detail || issue.code || "校验未通过。";
      return `${target}${detail}`;
    });
  }

  function errorLines(result, fallback) {
    const error = (result && result.error) || {};
    const lines = [];
    const code = error.code ? `错误代码：${error.code}` : "";
    const message = error.message || fallback || "操作失败。";
    lines.push(message);
    if (code) lines.push(code);
    const details = error.details;
    const cloudflare = details && details.cloudflare;
    if (cloudflare) {
      lines.push("Cloudflare HTTP " + cloudflare.http_status);
      for (const upstream of cloudflare.errors) {
        const prefix = "Cloudflare 错误 " + upstream.code;
        lines.push(upstream.message ? prefix + "：" + upstream.message : prefix);
      }
      if (cloudflare.ray_id) lines.push("CF-Ray " + cloudflare.ray_id);
    }
    if (details && Array.isArray(details.issues)) {
      issueLines(details.issues).forEach((line) => lines.push(line));
    }
    if (details && details.previously_withdrawn) {
      const withdrawn = details.previously_withdrawn;
      const contentCount = Array.isArray(withdrawn.content_ids)
        ? withdrawn.content_ids.length
        : 0;
      if (contentCount) {
        lines.push(`涉及 ${plural(contentCount, "曾撤回内容")}，需要二次确认。`);
      }
    }
    return lines;
  }

  function uncertainResult(node, what) {
    renderResult(node, {
      tone: "uncertain",
      title: "结果不确定",
      lines: [
        `${what}的请求可能已到达服务，也可能没有。为避免重复副作用，系统不会自动重试。`,
        "请刷新历史并核对实际状态，再决定下一步。",
      ],
      actions: [{ label: "刷新并核对", onClick: () => refreshAll(true) }],
    });
  }

  // ------------------------------------------------------------------
  // Loading
  // ------------------------------------------------------------------
  async function refreshAll(showToastAfter) {
    if (state.busy) return;
    setBusy(true);
    const revisionNode = byId("release-revision");
    setText(revisionNode, "读取中…");
    const result = await api("GET", "/api/releases");
    if (!result.ok) {
      setText(revisionNode, "");
      renderCurrentError(result);
      setBusy(false);
      return;
    }
    applyReleaseList(result.data);
    const tasks = [
      loadContents(),
      loadHistory({ append: false }),
      loadDrift(),
      loadPublicationState(),
      loadPublishPlan(),
    ];
    if (state.activeId) {
      tasks.push(loadActiveManifest());
    } else {
      state.activeManifest = null;
    }
    await Promise.all(tasks);
    if (window.location.hash === "#one-click-title") byId("one-click-title").scrollIntoView();
    setBusy(false);
    state.loaded = true;
    if (showToastAfter) showToast("已刷新发布状态。");
  }

  function applyReleaseList(data) {
    state.releases = Array.isArray(data.releases) ? data.releases : [];
    state.activeId = data.active_release_id || null;
    state.activeSince = data.active_since || null;
    state.revision = data.registry_revision;
    state.expectedActiveId = state.activeId;
    const activeRecord = state.releases.find((item) => item.release_id === state.activeId);
    renderCurrent(activeRecord);
    setText(
      byId("release-revision"),
      state.revision === null || state.revision === undefined
        ? ""
        : `registry v${state.revision}`
    );
  }

  async function loadActiveManifest() {
    if (!state.activeId) {
      state.activeManifest = null;
      return;
    }
    const result = await api(
      "GET",
      `/api/releases/${encodeURIComponent(state.activeId)}`
    );
    state.activeManifest = result.ok ? result.data : null;
    const record = state.releases.find((item) => item.release_id === state.activeId);
    renderCurrent(record);
    renderWithdrawCandidates();
  }

  async function loadContents() {
    const result = await api("GET", "/api/content");
    if (!result.ok) {
      state.contents = [];
      renderPublishCandidates();
      return;
    }
    const items = Array.isArray(result.data.items) ? result.data.items : [];
    state.contents = items.filter(
      (item) =>
        item.state === "active" &&
        item.visibility === "public" &&
        item.visibility_selected === true
    );
    state.publishSelected.forEach((id) => {
      if (!state.contents.some((item) => item.id === id)) {
        state.publishSelected.delete(id);
      }
    });
    renderPublishCandidates();
  }

  async function loadHistory({ append }) {
    const statusNode = byId("release-history-status");
    if (!append) {
      state.cursor = null;
      state.history = [];
    }
    setText(statusNode, "读取发布历史…");
    const params = new URLSearchParams();
    params.set("limit", String(HISTORY_LIMIT));
    params.set("order", "desc");
    const status = byId("filter-status").value;
    const action = byId("filter-action").value;
    const content = byId("filter-content").value.trim();
    const since = byId("filter-since").value;
    const until = byId("filter-until").value;
    if (status) params.set("status", status);
    if (action) params.set("action", action);
    if (content) params.set("content_id", content);
    const sinceIso = localInputToIso(since);
    const untilIso = localInputToIso(until);
    if (sinceIso) params.set("since", sinceIso);
    if (untilIso) params.set("until", untilIso);
    if (append && state.cursor) params.set("cursor", state.cursor);

    const result = await api("GET", `/api/releases/history?${params.toString()}`);
    if (!result.ok) {
      setText(statusNode, result.error.message || "读取历史失败。");
      statusNode.classList.add("error");
      renderHistory();
      return;
    }
    statusNode.classList.remove("error");
    const data = result.data;
    const items = Array.isArray(data.items) ? data.items : [];
    state.history = append ? state.history.concat(items) : items;
    state.cursor = data.next_cursor || null;
    state.historyTotal = Number(data.total) || state.history.length;
    state.historyLoaded = true;
    state.historyError = null;
    if (state.filteredEmpty()) {
      setText(statusNode, "没有符合筛选条件的 Release。");
    } else {
      setText(
        statusNode,
        `共 ${state.historyTotal} 条 Release，按最新在前排列。`
      );
    }
    renderHistory();
    populateRestoreSources();
  }

  state.filteredEmpty = () =>
    state.historyLoaded && state.history.length === 0;

  // ------------------------------------------------------------------
  // Current status
  // ------------------------------------------------------------------
  function renderCurrent(activeRecord) {
    const chip = byId("release-current-chip");
    const body = byId("release-current-body");
    if (!body) return;
    clearNode(body);
    if (state.activeId) {
      if (chip) {
        chip.className = "state-chip ok";
        setText(chip, "本机发布成功");
      }
      const grid = el("div", "release-current-grid");
      grid.appendChild(fact("本机发布时间", fmtTime(state.activeSince)));

      const record = activeRecord || state.releases.find((item) => item.release_id === state.activeId);
      const contentCount = record && Array.isArray(record.content_ids)
        ? record.content_ids.length
        : state.activeManifest && Array.isArray(state.activeManifest.record.content_ids)
          ? state.activeManifest.record.content_ids.length
          : "—";
      const attachmentCount = record && Array.isArray(record.attachment_ids)
        ? record.attachment_ids.length
        : state.activeManifest && Array.isArray(state.activeManifest.record.attachment_ids)
          ? state.activeManifest.record.attachment_ids.length
          : "—";
      grid.appendChild(fact("公开内容", String(contentCount)));
      grid.appendChild(fact("公开附件", String(attachmentCount)));
      body.appendChild(grid);

      const actions = el("div", "release-actions");
      const detail = el("button", "ghost-button", "高级版本信息");
      detail.type = "button";
      detail.addEventListener("click", () => openDetail(state.activeId));
      actions.appendChild(detail);
      const advanced = el("details");
      advanced.appendChild(el("summary", null, "当前版本详情"));
      advanced.appendChild(actions);
      body.appendChild(advanced);
      return;
    }

    if (chip) {
      chip.className = "state-chip conflict";
      const withdrawn = state.releases.some((item) => item.status === "withdrawn");
      setText(chip, withdrawn ? "已从本机撤回" : "尚未公开");
    }
    body.appendChild(
      el(
        "p",
        "release-current-empty",
        "本机目前没有公开网站。"
      )
    );
    const prepared = state.releases.filter((item) => item.status === "prepared");
    if (prepared.length) {
      const note = el(
        "p",
        "release-note",
        `有 ${prepared.length} 个尚未发布的本机版本，可在发布历史中查看。`
      );
      body.appendChild(note);
    }

  }

  function fact(key, value, mono) {
    const node = el("div", "release-fact");
    node.appendChild(el("div", "release-fact-key", key));
    node.appendChild(el("div", `release-fact-value${mono ? " mono" : ""}`, value));
    return node;
  }

  function renderCurrentError(result) {
    const body = byId("release-current-body");
    if (!body) return;
    clearNode(body);
    body.appendChild(el("p", "release-note error", result.error.message || "读取当前公开状态失败。"));
  }

  // ------------------------------------------------------------------
  // Publication drift (read-only truth check)
  // ------------------------------------------------------------------
  async function loadDrift() {
    const result = await api("GET", "/api/publication/drift");
    state.drift = result.ok ? result.data : null;
    state.driftError = result.ok
      ? ""
      : (result.error && result.error.message) || "读取公开副本一致性失败。";
    renderDrift();
  }

  function renderDrift() {
    const body = byId("release-drift-body");
    const chip = byId("release-drift-chip");
    if (!body) return;
    clearNode(body);

    if (!state.drift) {
      if (chip) {
        chip.className = "state-chip conflict";
        setText(chip, "无法读取");
      }
      body.appendChild(el("p", "release-note error", state.driftError || "读取失败。"));
      return;
    }

    const report = state.drift;
    const risk = Number(report.privacy_risk_count || 0);
    const stale = Number(report.stale_count || 0);

    if (chip) {
      if (report.state === "uncertain") {
        chip.className = "state-chip conflict";
        setText(chip, "不确定");
      } else if (risk > 0) {
        chip.className = "state-chip conflict";
        setText(chip, `隐私风险 ${risk}`);
      } else if (stale > 0) {
        chip.className = "state-chip";
        setText(chip, `快照过期 ${stale}`);
      } else {
        chip.className = "state-chip ok";
        setText(chip, "一致");
      }
    }

    if (report.state === "uncertain") {
      body.appendChild(
        el(
          "p",
          "release-note error",
          "无法完整校验当前公开副本，差异结论不可信。请先核对 Release 完整性与数据库可读性。"
        )
      );
    }
    for (const warning of report.warnings || []) {
      body.appendChild(el("p", "release-note error", warning.detail || warning.code));
    }

    const grid = el("div", "release-current-grid");
    grid.appendChild(fact("Active Release", report.active_release_id || "无", true));
    grid.appendChild(fact("Release 总数", String(report.release_count || 0)));
    grid.appendChild(
      fact(
        "Bundle 校验",
        report.bundle_verified === null || report.bundle_verified === undefined
          ? "—"
          : report.bundle_verified
            ? "通过"
            : "失败"
      )
    );
    grid.appendChild(fact("隐私风险项", String(risk)));
    grid.appendChild(fact("快照过期项", String(stale)));
    body.appendChild(grid);

    const groups = [
      ["PUBLISHED_BUT_NOW_PRIVATE", report.contents],
      ["PUBLISHED_BUT_DELETED", report.contents],
      ["PUBLISHED_BUT_DESELECTED", report.contents],
      ["PUBLISHED_SNAPSHOT_STALE", report.contents],
      ["SELECTED_BUT_NOT_PUBLISHED", report.contents],
      ["NOT_IN_ACTIVE_RELEASE", report.contents],
    ];
    let rendered = 0;
    for (const [category, rows] of groups) {
      const items = (rows || []).filter((row) => row.category === category);
      if (!items.length) continue;
      rendered += items.length;
      const section = el("div", `drift-group ${DRIFT_SEVERITY[category] || "info"}`);
      section.appendChild(
        el("div", "drift-group-title", `${DRIFT_LABELS[category] || category} · ${items.length}`)
      );
      for (const item of items) {
        const line = el("div", "drift-row");
        line.appendChild(
          el("span", "drift-row-title", item.current_title || item.published_title || "未命名")
        );
        line.appendChild(el("span", "drift-row-id mono", item.content_id || ""));
        if (item.detail) line.appendChild(el("span", "drift-row-detail", item.detail));
        section.appendChild(line);
      }
      body.appendChild(section);
    }

    const attachments = report.attachments || [];
    if (attachments.length) {
      rendered += attachments.length;
      const section = el("div", "drift-group risk");
      section.appendChild(
        el("div", "drift-group-title", `附件不一致 · ${attachments.length}`)
      );
      for (const item of attachments) {
        const line = el("div", "drift-row");
        line.appendChild(
          el("span", "drift-row-title", item.published_filename || item.attachment_id || "")
        );
        line.appendChild(el("span", "drift-row-id mono", item.content_id || ""));
        line.appendChild(el("span", "drift-row-detail", (item.reasons || []).join("；")));
        section.appendChild(line);
      }
      body.appendChild(section);
    }

    if (!rendered && report.state !== "uncertain") {
      body.appendChild(
        el(
          "p",
          "release-note",
          report.active_release_id
            ? "当前公开副本与工作区状态一致，没有发现隐私风险或快照过期。"
            : "当前没有任何公开版本，因此不存在公开副本差异。"
        )
      );
    }

    const actions = el("div", "release-actions");
    const goto = el("a", "ghost-button", "前往“发布与撤回”处理差异");
    goto.href = "#release-actions-title";
    actions.appendChild(goto);
    body.appendChild(actions);
  }

  // ------------------------------------------------------------------
  // History
  // ------------------------------------------------------------------
  function renderHistory() {
    const list = byId("release-history-list");
    if (!list) return;
    clearNode(list);
    if (!state.history.length) {
      list.appendChild(el("p", "release-empty", "暂无发布记录。"));
      setText(byId("release-history-more"), "");
      byId("release-history-more").classList.add("hidden");
      setText(byId("release-history-count"), "");
      return;
    }
    state.history.forEach((item) => {
      const row = el("div", "release-row");
      const main = el("div", "release-row-main");
      const title = el("div", "release-row-title");
      const status = el("span", `release-badge ${item.status || ""}`, statusLabel(item.status));
      const operation = el(
        "span",
        `release-badge ${operationOf(item)}`,
        actionLabel(operationOf(item))
      );
      title.appendChild(status);
      title.appendChild(operation);
      if (item.release_id === state.activeId) {
        title.appendChild(el("span", "release-badge active", "当前公开"));
      }
      main.appendChild(title);

      const meta = el("div", "release-row-meta");
      meta.appendChild(el("span", null, fmtTime(item.created_at)));
      const contentCount = Array.isArray(item.content_ids) ? item.content_ids.length : 0;
      const attachmentCount = Array.isArray(item.attachment_ids) ? item.attachment_ids.length : 0;
      meta.appendChild(el("span", null, `内容 ${contentCount}`));
      meta.appendChild(el("span", null, `附件 ${attachmentCount}`));
      if (item.reason) {
        meta.appendChild(el("span", null, `备注 ${item.reason}`));
      }
      main.appendChild(meta);
      row.appendChild(main);

      const actions = el("div", "release-row-actions");
      const detail = el("button", "ghost-button", "详情");
      detail.type = "button";
      detail.addEventListener("click", () => openDetail(item.release_id));
      actions.appendChild(detail);
      row.appendChild(actions);
      list.appendChild(row);
    });

    const more = byId("release-history-more");
    if (more) {
      if (state.cursor) {
        more.classList.remove("hidden");
        setText(more, "载入更多");
        more.disabled = false;
      } else {
        more.classList.add("hidden");
      }
    }
    setText(byId("release-history-count"), `已显示 ${state.history.length} / ${state.historyTotal}`);
    setText(byId("release-history-total"), state.historyTotal ? `共 ${state.historyTotal} 条` : "");
  }

  // ------------------------------------------------------------------
  // Publish
  // ------------------------------------------------------------------
  function renderPublishCandidates() {
    const container = byId("publish-candidates");
    if (!container) return;
    clearNode(container);
    if (!state.contents.length) {
      container.appendChild(
        el(
          "p",
          "release-empty",
          "没有可发布的公开内容。请先在内容页将内容设为公开，并保持 active 状态。"
        )
      );
      syncPublishButtons();
      return;
    }
    state.contents.forEach((content) => {
      const card = el("div", "release-pick");
      const head = el("div", "release-pick-head");
      const check = el("label", "release-check");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.setAttribute("aria-label", `选择 ${content.title}`);
      checkbox.checked = state.publishSelected.has(content.id);
      checkbox.addEventListener("change", () => togglePublishContent(content.id, checkbox.checked));
      check.appendChild(checkbox);
      head.appendChild(check);

      const main = el("div", "release-pick-main");
      main.appendChild(el("div", "release-pick-title", content.title || "（无标题）"));
      const meta = el(
        "div",
        "release-pick-meta",
        `${content.id} · 修订 v${content.revision ?? "—"} · 更新 ${fmtTime(content.updated_at)}`
      );
      main.appendChild(meta);
      const excerpt = (content.body || "").trim().slice(0, 140);
      if (excerpt) {
        main.appendChild(el("div", "release-pick-body", excerpt + ((content.body || "").length > 140 ? "…" : "")));
      }

      const detail = state.contentDetails.get(content.id);
      if (detail) {
        const attachments = (detail.attachments || []).filter(
          (item) =>
            String(item.state || "").toLowerCase() === "ready" &&
            item.visibility === "public"
        );
        if (attachments.length) {
          const wrapper = el("div", "release-pick-attachments");
          attachments.forEach((attachment) => {
            const label = el("label", "release-pick-attachment");
            const attachCheck = document.createElement("input");
            attachCheck.type = "checkbox";
            attachCheck.checked =
              state.publishSelected.has(content.id) &&
              state.publishAttachments.has(attachment.id);
            attachCheck.disabled = !state.publishSelected.has(content.id);
            attachCheck.setAttribute("aria-label", `选择附件 ${attachment.filename}`);
            attachCheck.addEventListener("change", () =>
              togglePublishAttachment(content.id, attachment.id, attachCheck.checked)
            );
            label.appendChild(attachCheck);
            label.appendChild(
              el(
                "span",
                null,
                `${attachment.filename} · ${fmtBytes(attachment.byte_size)}`
              )
            );
            wrapper.appendChild(label);
          });
          main.appendChild(wrapper);
        } else {
          main.appendChild(
            el("div", "release-pick-attachments", "无公开附件")
          );
        }
      } else if (state.publishSelected.has(content.id)) {
        main.appendChild(
          el("div", "release-pick-attachments", "正在读取附件…")
        );
      }

      head.appendChild(main);
      card.appendChild(head);
      container.appendChild(card);
    });
    syncPublishButtons();
  }

  async function togglePublishContent(contentId, checked) {
    if (checked) {
      state.publishSelected.add(contentId);
      await ensureContentDetail(contentId);
      const detail = state.contentDetails.get(contentId);
      if (detail) {
        (detail.attachments || [])
          .filter(
            (item) =>
              String(item.state || "").toLowerCase() === "ready" &&
              item.visibility === "public"
          )
          .forEach((item) => state.publishAttachments.add(item.id));
      }
    } else {
      state.publishSelected.delete(contentId);
      const detail = state.contentDetails.get(contentId);
      if (detail) {
        (detail.attachments || []).forEach((item) => state.publishAttachments.delete(item.id));
      }
    }
    state.previewValid = false;
    resetResult(byId("publish-preview-result"));
    renderPublishCandidates();
    updatePublishSummary();
  }

  function togglePublishAttachment(contentId, attachmentId, checked) {
    if (!state.publishSelected.has(contentId)) return;
    if (checked) state.publishAttachments.add(attachmentId);
    else state.publishAttachments.delete(attachmentId);
    state.previewValid = false;
    resetResult(byId("publish-preview-result"));
    updatePublishSummary();
  }

  async function ensureContentDetail(contentId) {
    if (state.contentDetails.has(contentId)) return;
    const result = await api(
      "GET",
      `/api/content/${encodeURIComponent(contentId)}`
    );
    if (result.ok) state.contentDetails.set(contentId, result.data);
    else state.contentDetails.set(contentId, { attachments: [] });
  }

  function updatePublishSummary() {
    const contentCount = state.publishSelected.size;
    const attachmentCount = state.publishAttachments.size;
    const node = byId("publish-selection-summary");
    if (!node) return;
    if (!contentCount) {
      setText(node, "尚未选择内容。发布必须显式选择至少一个公开内容。");
      return;
    }
    setText(
      node,
      `已选择 ${plural(contentCount, "内容")}、${plural(attachmentCount, "附件")}。发布将创建新的 Release 并切换公开视图。`
    );
  }

  function syncPublishButtons() {
    const previewButton = byId("publish-preview-button");
    const submitButton = byId("publish-submit-button");
    if (!previewButton || !submitButton) return;
    const hasSelection = state.publishSelected.size > 0;
    previewButton.disabled = state.busy || !hasSelection;
    submitButton.disabled = state.busy || !hasSelection || !state.previewValid;
    updatePublishSummary();
  }

  async function previewPublish() {
    if (state.busy || !state.publishSelected.size) return;
    const node = byId("publish-preview-result");
    renderResult(node, { tone: "pending", title: "正在生成只读预览…" });
    const result = await api("POST", "/api/releases/preview", {
      content_ids: Array.from(state.publishSelected),
      attachment_ids: Array.from(state.publishAttachments),
    });
    if (!result.ok) {
      state.previewValid = false;
      syncPublishButtons();
      renderResult(node, {
        tone: "error",
        title: "预览未通过",
        lines: errorLines(result, "预览请求失败。"),
      });
      return;
    }
    const data = result.data;
    state.previewValid = Boolean(data.valid);
    syncPublishButtons();
    if (!data.valid) {
      renderResult(node, {
        tone: "error",
        title: "预览发现阻断问题",
        lines: [
          "以下问题会阻止发布；请调整选择后重新预览。",
          ...issueLines(data.issues),
          "本预览没有生成 Bundle、没有修改注册表、没有写入事件或数据库。",
        ],
      });
      return;
    }

    const totals = data.totals || {};
    const delta = data.delta || {};
    const lines = [
      `内容 ${totals.content_count ?? 0} 个，附件 ${totals.attachment_count ?? 0} 个，附件体积 ${fmtBytes(totals.attachment_bytes)}。`,
      `与当前公开版本相比：新增内容 ${(delta.added_content_ids || []).length} 个，移除内容 ${(delta.removed_content_ids || []).length} 个，新增附件 ${(delta.added_attachment_ids || []).length} 个。`,
      delta.same_as_active
        ? "该选择与当前公开版本完全相同，发布不会改变公开内容。"
        : "发布后将立即切换公开视图，访客只能看到该 Release 中的内容。",
      "只读预览：未生成 Bundle、未修改注册表、未写入事件、未修改数据库或附件。",
    ];
    if ((delta.removed_content_ids || []).length) {
      lines.push(`将被移出公开视图的内容：${delta.removed_content_ids.join("、")}`);
    }
    renderResult(node, {
      tone: "ok",
      title: "预览有效 · 待确认发布",
      lines,
    });
  }

  // ------------------------------------------------------------------
  // Unified publication state
  // ------------------------------------------------------------------
  function renderPublicationState() {
    const node = byId("one-click-state");
    if (!node) return;
    clearNode(node);
    const data = state.publication;
    if (!data) {
      setText(node, "发布状态暂时无法读取。");
      return;
    }
    if (data.release?.state === "uncertain" || data.release?.withdrawal_required) {
      const chip = byId("release-current-chip");
      chip.className = "state-chip risk";
      setText(chip, "本机公开状态待核对");
    }
    if (data.last_failed_attempt) {
      const failed = el("p", "release-note error");
      setText(
        failed,
        "上次本机发布未完成。请核对待发布的内容后重试。"
      );
      node.appendChild(failed);
    }
  }

  async function loadPublicationState() {
    const result = await api("GET", "/api/publication/state");
    state.publication = result.ok ? result.data : null;
    renderPublicationState();
  }

  // ------------------------------------------------------------------
  // One-click local publishing
  // ------------------------------------------------------------------
  function syncOneClickButton() {
    const button = byId("one-click-publish");
    if (!button) return;
    const plan = state.publishPlan;
    const disabled = state.busy || !plan || plan.empty || !plan.valid;
    const deployment = plan?.deployment || {};
    const retryWithdrawal = deployment.can_publish_withdrawal_successor
      && plan?.active_release_id !== deployment.last_attempt?.release_id;
    const removal = removalIds(plan).size > 0 || plan?.delta.removed_attachment_ids?.length > 0
      || deployment.status === "cleanup_pending" || retryWithdrawal;
    const retry = ["failed", "waiting", "cleanup_pending"].includes(deployment.status);
    const needsUpdate = plan && (plan.delta.has_changes || retry);
    const allowEmpty = plan?.empty && (removal || (retry && deployment.last_attempt));
    const blocked = ["unconfirmed", "unavailable"].includes(deployment.status) || deployment.production_unknown;
    const withdrawalSuccessor = deployment.can_publish_withdrawal_successor
      && (plan?.delta.removed_content_ids?.length || plan?.delta.removed_attachment_ids?.length || retryWithdrawal)
      && !plan?.delta.added_content_ids?.length && !plan?.delta.added_attachment_ids?.length;
    button.textContent = removal ? "从公开网站移除" : deployment.status === "failed" && !plan?.delta.has_changes ? "重试上线" : "发布到网站";
    button.disabled = state.busy || (blocked && !withdrawalSuccessor)
      || (!needsUpdate && !withdrawalSuccessor) || (disabled && !allowEmpty);
    const redeploy = byId("one-click-redeploy");
    redeploy.classList.toggle("hidden", !deployment.target_id);
    redeploy.disabled = disabled || blocked || removal || plan.delta.has_changes;
    const sync = byId("hosting-sync");
    sync.classList.toggle("hidden", !deployment.target_id);
    sync.disabled = state.busy || blocked || !(state.activeId || deployment.last_attempt) || deployment.status === "succeeded";
    byId("one-click-force").disabled = disabled;
    byId("one-click-preview-draft").disabled = disabled;
    byId("hosting-save").disabled = state.busy;
    byId("hosting-list").disabled = state.busy;
    const check = byId("hosting-check");
    if (check) check.disabled = state.busy;
    byId("one-click-preview-link").classList.toggle("hidden", !state.activeId);
  }

  function removalIds(plan) {
    return new Set([
      ...(plan?.delta.removed_content_ids || []),
      ...(plan?.deployment?.pending_removal_ids || []),
    ]);
  }

  function renderPublishPlan() {
    const data = state.publishPlan;
    const chip = byId("one-click-chip");
    const planNode = byId("one-click-plan");
    const changesNode = byId("one-click-changes");
    if (!data || !planNode || !changesNode) return;
    clearNode(planNode);
    renderHosting(data.deployment || {});

    if (data.empty) {
      const removal = removalIds(data).size;
      if (chip) {
        chip.className = "state-chip";
        setText(chip, removal ? "等待从公开网站移除" : "无可发布内容");
      }
      planNode.appendChild(
        el(
          "p",
          "release-note",
          removal
            ? `公开网站仍可能有 ${removal} 条待移除内容。请确认更新网站。`
            : "当前没有选择公开的内容。请先在内容编辑中把条目的可见性设为『公开』。"
        )
      );
      setText(changesNode, "");
      syncOneClickButton();
      return;
    }

    const totals = data.totals || {};
    const delta = data.delta || {};
    if (!data.valid) {
      chip.className = "state-chip risk";
      setText(chip, "需要处理");
    } else if (!data.active_release_id) {
      chip.className = "state-chip warn";
      setText(chip, "已选择公开，等待发布");
    } else if (delta.has_changes) {
      chip.className = "state-chip warn";
      setText(chip, removalIds(data).size ? "等待从公开网站移除" : "内容已修改，等待更新");
    } else {
      chip.className = "state-chip ok";
      setText(chip, "已与当前公开版本一致");
    }

    const grid = el("div", "release-current-grid");
    grid.appendChild(
      fact("将公开内容", `${totals.content_count ?? data.content_ids.length} 条`)
    );
    grid.appendChild(
      fact("将公开附件", `${totals.attachment_count ?? 0} 个`)
    );
    planNode.appendChild(grid);

    const parts = [];
    if (!data.active_release_id) {
      parts.push("这将是第一个公开版本。");
    }
    parts.push(
      `新增内容 ${(delta.added_content_ids || []).length} 个，` +
        `移出内容 ${(delta.removed_content_ids || []).length} 个，` +
        `内容有修改 ${(delta.changed_content_ids || []).length} 个，` +
        `新增附件 ${(delta.added_attachment_ids || []).length} 个。`
    );
    if (!delta.has_changes && data.active_release_id) {
      parts.push("本机网站已是最新。");
    }
    if ((data.skipped_attachments || []).length) {
      parts.push(
        `有 ${data.skipped_attachments.length} 个附件因私有或未就绪不会被公开。`
      );
    }
    setText(changesNode, parts.join(" "));

    if (!data.valid) {
      const issueNode = el("p", "release-note");
      setText(issueNode, `发布前校验未通过：${issueLines(data.issues).join("；")}`);
      issueNode.classList.add("error");
      planNode.appendChild(issueNode);
    }
    syncOneClickButton();
  }

  async function loadPublishPlan() {
    const chip = byId("one-click-chip");
    const result = await api("GET", "/api/publish/plan");
    if (!result.ok) {
      state.publishPlan = null;
      if (chip) {
        chip.className = "state-chip risk";
        setText(chip, "读取失败");
      }
      renderResult(byId("one-click-result"), {
        tone: "error",
        title: "无法读取可发布内容",
        lines: errorLines(result, "读取发布计划失败。"),
      });
      syncOneClickButton();
      return;
    }
    if (state.publishPlan?.confirmation !== result.data.confirmation) resetResult(byId("one-click-preview-draft-result"));
    state.publishPlan = result.data;
    renderPublishPlan();
  }

  async function previewDraft() {
    const plan = state.publishPlan;
    if (!plan || !plan.valid) return;
    const node = byId("one-click-preview-draft-result");
    setBusy(true);
    renderResult(node, { tone: "pending", title: "正在生成预览…" });
    const result = await api("POST", "/api/publish/preview-site", { confirmation: plan.confirmation });
    setBusy(false);
    if (!result.ok) {
      renderResult(node, {
        tone: "error", title: "暂时无法预览", lines: errorLines(result, "请刷新后重试。"),
      });
      return;
    }
    renderResult(node, { tone: "ok", title: "网站预览可用" });
    const link = el("a", "ghost-button", "打开待发布网站预览");
    link.href = result.data.url;
    link.target = "_blank";
    link.rel = "noopener";
    node.appendChild(link);
  }

  const HOSTING_LABELS = {
    not_configured: "线上托管尚未连接",
    waiting: "等待线上部署",
    succeeded: "线上部署成功",
    failed: "线上部署失败",
    unconfirmed: "等待核对线上部署",
    cleanup_pending: "生产网址已更新，旧公开地址待移除",
    unavailable: "暂时无法读取线上发布状态",
  };

  function renderHosting(deployment) {
    const node = byId("hosting-state");
    clearNode(node);
    node.appendChild(el("p", "release-note", HOSTING_LABELS[deployment.status] || HOSTING_LABELS.unavailable));
    if (deployment.target_id) {
      byId("hosting-token").required = false;
      const account = byId("hosting-account");
      const project = byId("hosting-project");
      if (!account.value) account.value = deployment.account;
      if (!project.value) project.value = deployment.project;
      node.appendChild(el("p", "release-note", "目标网址 " + deployment.target_url));
      if (deployment.public_url) {
        const link = el("a", "ghost-button", "打开公开网站");
        link.href = deployment.public_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        node.appendChild(link);
      }
      const check = el("button", "ghost-button", "核对连接与线上结果");
      check.id = "hosting-check";
      check.type = "button";
      check.addEventListener("click", () => updateHosting());
      node.appendChild(check);
    }
    if (deployment.last_attempt?.message) {
      node.appendChild(el("p", "release-note", deployment.last_attempt.message));
    }
    const progress = deployment.verification;
    if (progress) {
      if (deployment.last_attempt?.deployment_id) {
        node.appendChild(el("p", "release-note", progress.canonical_confirmed
          ? "新部署已成为生产版本" : "新部署已创建，当前生产版本待核对"));
      }
      if (progress.positive_verified) node.appendChild(el("p", "release-note", "新版公开资源已核验"));
      if (progress.retirement_required) {
        const cacheMessages = {
          UNSUPPORTED: "当前托管不支持应用已验证的缓存清理。下线结果以公开访问复验为准。",
          UNKNOWN: "缓存处置能力或结果待确认。",
          TIMEOUT: "缓存处置结果查询超时。",
          FAILED: "缓存处置失败。",
          COMPLETED: "已收到缓存处置结果，公开访问仍需复验。",
        };
        const cache = cacheMessages[progress.cache_status];
        if (cache) node.appendChild(el("p", "release-note", cache));
        const pops = progress.blocking_pops || [];
        const retirement = progress.production_retired ? "旧生产网址已通过多点下线复验"
          : "旧生产网址仍待下线复验（" + progress.clean_rounds + "/" + progress.required_rounds + " 轮）";
        node.appendChild(el("p", "release-note", retirement + (pops.length ? " · 待复验节点 " + pops.join("、") : "")));
        const observations = progress.recent_observations || [];
        const failed = observations.filter((row) => row.old_bytes || row.error ||
          (row.cache_status === "HIT" && row.http_status === 200 && row.method === "HEAD")).slice(-4);
        for (const row of failed) {
          node.appendChild(el("p", "release-note error",
            [row.method, row.path, row.pop, row.http_status || "网络失败", row.cache_status, row.ray_id]
              .filter(Boolean).join(" · ")));
        }
      }
      for (const alias of progress.aliases || []) {
        const aliasLabels = {
          accessible: "历史部署仍可匿名访问", present: "历史部署仍存在", verifying: "历史地址正在复验",
          retired: "历史部署已下线", unknown: "历史部署归属待确认", unconfirmed: "历史地址下线结果待核对",
        };
        node.appendChild(el("p", "release-note", [
          aliasLabels[alias.status], alias.id, alias.protected ? "受保护" : "",
          alias.delete_requested && alias.status !== "retired" ? "已请求移除，等待回执及复验" : "",
        ].filter(Boolean).join(" · ")));
        if (alias.approval && deployment.status === "cleanup_pending") {
          const cleanup = el("button", "ghost-button", "审核并移除该旧部署");
          cleanup.type = "button";
          cleanup.addEventListener("click", () => approveHostedCleanup(deployment, alias));
          node.appendChild(cleanup);
        }
      }
      if (progress.complete) node.appendChild(el("p", "release-note", "已完成本次线上核验"));
    }
    const verification = deployment.last_attempt?.verification_error;
    if (verification) {
      const details = [
        verification.method,
        Number.isInteger(verification.http_status) ? ("HTTP " + verification.http_status) : "",
        Number.isInteger(verification.cloudflare_code) ? ("Cloudflare 错误 " + verification.cloudflare_code) : "",
        verification.ray_id ? ("CF-Ray " + verification.ray_id) : "",
      ].filter(Boolean);
      node.appendChild(el("p", "release-note", details.join(" · ")));
    }
    if (deployment.connection_error || deployment.production_unknown) {
      node.appendChild(el("p", "release-note error", deployment.connection_error ||
        "当前生产网址与本机记录不一致，线上状态待核对。"));
    }
    if (deployment.pending_removal_ids?.length) {
      node.appendChild(el("p", "release-note error",
        "线上仍可能访问待移除的内容。核对生产网址后，历史部署需单独批准才能移除。"));
    }
    const history = byId("hosting-history-list");
    clearNode(history);
    byId("hosting-history").classList.toggle("hidden", !deployment.history?.length);
    for (const attempt of deployment.history || []) {
      const item = el("p", "release-note",
        [fmtTime(attempt.started_at), HOSTING_LABELS[attempt.status] || "待核对", attempt.deleted ? "旧线上地址已移除" : ""]
          .filter(Boolean).join(" · "));
      const detail = el("details");
      detail.appendChild(el("summary", null, "版本与部署编号"));
      detail.appendChild(el("p", "release-note", [attempt.release_id, attempt.deployment_id].filter(Boolean).join(" · ")));
      history.append(item, detail);
    }
  }

  async function approveHostedCleanup(deployment, alias) {
    if (state.busy) return;
    const active = state.activeId;
    const confirmed = await confirmDialog({
      title: "确认移除指定的历史部署？",
      message: ["项目 " + deployment.project, "部署 " + alias.id, "创建于 " + fmtTime(alias.created_at),
        "来源为本应用发布", "独立地址 https://" + alias.host,
        "移除后该独立地址和回滚点将失效，随后会复验生产网址与独立地址。"].join("\n"),
      confirmLabel: "批准移除该部署",
    });
    if (!confirmed) return;
    setBusy(true);
    const result = await api("POST", "/api/publish/cloudflare/cleanup", {
      confirm: true, expected_active: active, target_id: deployment.target_id,
      deployment_id: alias.id, approval: alias.approval,
    });
    setBusy(false);
    showToast(result.ok ? result.data.last_attempt?.message || HOSTING_LABELS[result.data.status]
      : errorLines(result, "旧部署未能移除。").join(" "), result.ok ? "ok" : "error");
    await refreshAll(false);
  }

  async function updateHosting(configuration) {
    if (state.busy) return;
    setBusy(true);
    const feedback = byId("hosting-feedback");
    setText(feedback, "正在核对 Cloudflare 连接…");
    const result = await api("POST",
      "/api/publish/cloudflare/" + (configuration ? "configure" : "check"), configuration);
    byId("hosting-token").value = "";
    const deployment = result.data;
    const verificationStatus = !configuration && deployment?.status;
    const verificationPending = verificationStatus === "unconfirmed" || verificationStatus === "failed";
    const verificationMessage = verificationPending || ["succeeded", "cleanup_pending"].includes(verificationStatus)
      ? deployment.last_attempt?.message || HOSTING_LABELS[verificationStatus]
      : "";
    const message = result.ok
      ? deployment.connection_error || verificationMessage || ("连接已核对 · " + fmtTime(deployment.checked_at))
      : errorLines(result, "无法连接 Cloudflare。").join(" ");
    setText(feedback, message);
    showToast(message, result.ok && !deployment.connection_error && !verificationPending ? "ok" : "error");
    if (result.ok && !configuration) resetResult(byId("one-click-result"));
    setBusy(false);
    await refreshAll(false);
  }

  async function syncHostedSnapshot() {
    const deployment = state.publishPlan?.deployment;
    if (state.busy || !deployment?.target_id) return;
    const active = state.activeId;
    const confirmed = await confirmDialog({
      title: "确认同步到线上？",
      message: "将用当前本机网站更新 " + deployment.target_url + "。历史部署的移除需单独批准。",
      confirmLabel: "确认并同步",
    });
    if (!confirmed) return;
    setBusy(true);
    const result = await api("POST", "/api/publish/cloudflare/sync", {
      confirm: true, expected_active: active, target_id: deployment.target_id,
    });
    setBusy(false);
    showToast(result.ok ? HOSTING_LABELS[result.data.status] : errorLines(result, "同步未完成。").join(" "),
      result.ok && result.data.status === "succeeded" ? "ok" : "error");
    await refreshAll(false);
  }

  async function publishOneClick(mode = "default") {
    const data = state.publishPlan;
    if (state.busy || !data) return;
    const deployment = data.deployment || {};
    const online = mode !== "local" && Boolean(deployment.target_id);
    const force = mode !== "default";
    const ids = removalIds(data);
    const retryWithdrawal = deployment.can_publish_withdrawal_successor
      && data.active_release_id !== deployment.last_attempt?.release_id;
    const removal = mode === "default" && (ids.size > 0 || data.delta.removed_attachment_ids?.length > 0
      || deployment.status === "cleanup_pending" || retryWithdrawal);
    const canPublishEmpty = data.empty && (removal || (online && deployment.last_attempt));
    if (!data.valid && !canPublishEmpty) return;
    const noChanges = data.delta.has_changes === false && Boolean(data.active_release_id);
    const republish = data.previously_withdrawn?.length || 0;
    const lines = [];
    if (mode === "local" && noChanges) lines.push("继续发布会生成一个内容相同的新版本。");
    if (republish) lines.push("此操作会重新公开 " + republish + " 条曾撤回的内容。");
    if (ids.size) lines.push("有 " + ids.size + " 条内容待从公开网站移除。");
    lines.push("发布后网站包含 " + data.content_ids.length + " 条内容和 " + data.attachment_ids.length + " 个附件。");
    if (online) {
      lines.push("将更新本机网站并部署到 " + deployment.target_url + "。历史部署的移除需单独批准。");
    } else {
      lines.push("此次操作只更新本机网站。此前上传的线上副本仍可能可访问。");
    }
    const confirmed = await confirmDialog({
      title: removal ? "确认从公开网站移除？" : "确认发布到网站",
      message: lines.join("\n"),
      confirmLabel: removal ? "确认移除" : force ? "仍要重新发布" : "确认并发布",
    });
    if (!confirmed) return;

    const node = byId("one-click-result");
    setBusy(true);
    renderResult(node, { tone: "pending", title: online ? "正在发布并核对线上结果…" : "正在更新本机网站…" });
    const result = await api("POST", "/api/publish/one-click", {
      confirm: true, remove: removal, expected_active: data.active_release_id,
      confirm_republish_withdrawn: Boolean(republish), online, force,
      confirmation: data.confirmation,
    });
    setBusy(false);
    if (!result.ok) {
      if (result.uncertain) {
        uncertainResult(node, "发布");
      } else {
        renderResult(node, {
          tone: "error", title: "发布未完成",
          lines: errorLines(result, "请检查发布状态。"),
          actions: [{ label: "刷新状态", onClick: () => refreshAll(true) }],
        });
      }
      await refreshAll(false);
      return;
    }

    const info = result.data;
    const local = info.local_publish === "withdrawn" ? "本机网站已撤回。" : "本机发布成功。";
    const displayed = [local];
    if (online) {
      displayed.push(info.deployment.last_attempt?.message || "请在当前公开状态查看线上结果。");
      if (info.deployment.status !== "succeeded") displayed.push("此前的线上公开副本仍可能可访问。");
    } else {
      displayed.push("公开网站仅在本机更新。");
    }
    renderResult(node, {
      tone: online && info.deployment.status !== "succeeded" ? "error" : "ok",
      title: online ? HOSTING_LABELS[info.deployment.status] : local,
      lines: displayed,
    });
    await refreshAll(false);
  }

  async function publishSelection() {
    if (state.busy || !state.publishSelected.size || !state.previewValid) return;
    const contentIds = Array.from(state.publishSelected);
    const attachmentIds = Array.from(state.publishAttachments);
    const confirmed = await confirmDialog({
      title: "确认发布",
      message:
        `将创建新 Release 并立即公开 ${plural(contentIds.length, "内容")}、${plural(attachmentIds.length, "附件")}。` +
        "发布后访客将看到新的公开视图。此操作会写入发布历史，不能原地撤销，但可通过撤回或历史恢复调整。",
      confirmLabel: "确认并发布",
    });
    if (!confirmed) return;

    const node = byId("publish-preview-result");
    resetResult(byId("publish-preview-result"));
    setBusy(true);
    renderResult(node, { tone: "pending", title: "正在准备 Release（prepare）…" });

    const prepared = await api("POST", "/api/releases/prepare", {
      content_ids: contentIds,
      attachment_ids: attachmentIds,
    });
    if (!prepared.ok) {
      setBusy(false);
      if (prepared.uncertain) {
        uncertainResult(node, "准备 Release");
      } else {
        renderResult(node, {
          tone: "error",
          title: "发布未完成",
          lines: ["未生成已激活 Release。", ...errorLines(prepared, "prepare 失败。")],
        });
      }
      return;
    }
    const releaseId = prepared.data.release_id;
    renderResult(node, {
      tone: "pending",
      title: "已准备未公开 · 正在激活",
      lines: [
        `Release ${releaseId} 已生成，但尚未切换公开视图。`,
        prepared.data.audit_event_pending
          ? "注意：prepare 审计事件待补记，可稍后在完整性审计中核对。"
          : "准备事件已记录。",
      ],
    });

    const activated = await api(
      "POST",
      `/api/releases/${encodeURIComponent(releaseId)}/activate`,
      {}
    );
    setBusy(false);
    if (!activated.ok) {
      if (activated.uncertain) {
        renderResult(node, {
          tone: "uncertain",
          title: "已准备未公开 · 激活结果不确定",
          lines: [
            `Release ${releaseId} 已生成，但激活请求结果不确定。`,
            "不会自动重试。请刷新历史核对状态；若仍为已准备未公开，可在详情中显式激活。",
          ],
          actions: [
            { label: "刷新并核对", onClick: () => refreshAll(true) },
            { label: "查看详情", onClick: () => openDetail(releaseId) },
          ],
        });
      } else {
        renderResult(node, {
          tone: "error",
          title: "已准备未公开 · 激活失败",
          lines: [
            `Release ${releaseId} 已生成但未激活，公开视图没有改变。`,
            ...errorLines(activated, "activate 失败。"),
            "可在历史中打开该 Release 详情并重新激活。",
          ],
          actions: [
            { label: "查看详情", onClick: () => openDetail(releaseId) },
            { label: "刷新", onClick: () => refreshAll(true) },
          ],
        });
      }
      return;
    }

    const data = activated.data;
    const lines = [
      `Release ${data.release_id || releaseId} 已激活，公开视图已切换。`,
      data.previous_active_release_id
        ? `上一公开 Release：${data.previous_active_release_id}`
        : "此前没有公开 Release。",
    ];
    if (data.audit_event_pending) {
      lines.push("激活事件待补记：不影响公开内容，但需要在后续审计中核对。");
    }
    renderResult(node, {
      tone: data.audit_event_pending ? "pending" : "ok",
      title: data.audit_event_pending ? "发布成功 · 事件待补记" : "发布成功",
      lines,
      actions: [{ label: "查看 Release 详情", onClick: () => openDetail(data.release_id || releaseId) }],
    });
    state.previewValid = false;
    state.publishSelected.clear();
    state.publishAttachments.clear();
    await refreshAll(false);
    syncPublishButtons();
  }

  // ------------------------------------------------------------------
  // Withdraw
  // ------------------------------------------------------------------
  function activeContentItems() {
    if (state.activeManifest && Array.isArray(state.activeManifest.manifest.items)) {
      return state.activeManifest.manifest.items;
    }
    return [];
  }

  function renderWithdrawCandidates() {
    const container = byId("withdraw-candidates");
    if (!container) return;
    clearNode(container);
    const items = activeContentItems();
    if (!state.activeId) {
      container.appendChild(el("p", "release-empty", "当前没有公开 Release，无需撤回。"));
      syncWithdrawButton();
      return;
    }
    if (!items.length) {
      container.appendChild(el("p", "release-empty", "正在读取当前公开内容…"));
      syncWithdrawButton();
      return;
    }
    state.withdrawSelected.forEach((id) => {
      if (!items.some((item) => item.content_id === id)) state.withdrawSelected.delete(id);
    });
    items.forEach((item) => {
      const card = el("div", "release-pick");
      const head = el("div", "release-pick-head");
      const check = el("label", "release-check");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = state.withdrawSelected.has(item.content_id);
      checkbox.setAttribute("aria-label", `选择撤回 ${item.title}`);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) state.withdrawSelected.add(item.content_id);
        else state.withdrawSelected.delete(item.content_id);
        updateWithdrawSummary();
        syncWithdrawButton();
      });
      check.appendChild(checkbox);
      head.appendChild(check);
      const main = el("div", "release-pick-main");
      main.appendChild(el("div", "release-pick-title", item.title || "（无标题）"));
      main.appendChild(el("div", "release-pick-meta", item.content_id));
      const attachmentNames = (item.attachments || [])
        .map((attachment) => attachment.filename)
        .filter(Boolean);
      if (attachmentNames.length) {
        main.appendChild(
          el("div", "release-pick-body", `附件：${attachmentNames.join("、")}`)
        );
      }
      head.appendChild(main);
      card.appendChild(head);
      container.appendChild(card);
    });
    updateWithdrawSummary();
    syncWithdrawButton();
  }

  function updateWithdrawSummary() {
    const node = byId("withdraw-summary");
    if (!node) return;
    const total = activeContentItems().length;
    const selected = state.withdrawSelected.size;
    const allSelected = total > 0 && selected === total;
    if (!state.activeId) {
      setText(node, "");
      return;
    }
    if (!selected) {
      setText(node, `当前公开 ${plural(total, "内容")}。选择要撤回的内容；全选将执行整体撤回。`);
      return;
    }
    if (allSelected) {
      setText(
        node,
        `已选择全部 ${plural(total, "内容")}：将执行整体撤回，公开视图会清空。若只想下线部分内容，请取消部分选择。`
      );
      return;
    }
    setText(
      node,
      `将撤回 ${plural(selected, "内容")}，保留 ${plural(total - selected, "内容")} 公开；会生成新的后继 Release。`
    );
  }

  function syncWithdrawButton() {
    const button = byId("withdraw-submit-button");
    if (!button) return;
    const total = activeContentItems().length;
    const selected = state.withdrawSelected.size;
    const allSelected = total > 0 && selected === total;
    button.disabled = state.busy || !state.activeId || selected === 0;
    if (allSelected) {
      setText(button, "整体撤回全部内容");
    } else {
      setText(button, "撤回所选内容");
    }
  }

  async function submitWithdraw() {
    if (state.busy || !state.activeId || !state.withdrawSelected.size) return;
    const items = activeContentItems();
    const total = items.length;
    const selected = state.withdrawSelected.size;
    const allSelected = selected === total;
    const selectedTitles = items
      .filter((item) => state.withdrawSelected.has(item.content_id))
      .map((item) => item.title || item.content_id);
    const attachmentCount = items
      .filter((item) => state.withdrawSelected.has(item.content_id))
      .reduce((sum, item) => sum + (item.attachments || []).length, 0);

    const hosting = state.publishPlan?.deployment || {};
    const active = state.activeId;
    const target = hosting.target_id;
    const destination = target ? "\n将同步更新 " + hosting.target_url + " 并核对线上结果。" : "\n此次撤回作用于本机网站。";
    const confirmed = await confirmDialog({
      title: allSelected ? "确认整体撤回" : "确认部分撤回",
      message: (allSelected
        ? `将撤回全部 ${plural(total, "内容")}及其附件。`
        : `将撤回 ${plural(selected, "内容")}（${selectedTitles.join("、")}）和 ${attachmentCount} 个附件。`) + destination,
      confirmLabel: allSelected ? "确认整体撤回" : "确认撤回",
    });
    if (!confirmed) return;

    const node = byId("withdraw-result");
    resetResult(node);
    setBusy(true);
    renderResult(node, { tone: "pending", title: "正在撤回…" });
    const result = await api("POST", "/api/releases/withdraw", {
      content_ids: allSelected ? null : Array.from(state.withdrawSelected),
      attachment_ids: null,
      confirm: true,
      expected_active: active,
      target_id: target,
    });
    setBusy(false);
    if (!result.ok) {
      if (result.uncertain) {
        uncertainResult(node, "撤回");
      } else {
        renderResult(node, {
          tone: "error",
          title: "撤回未完成",
          lines: errorLines(result, "撤回失败。"),
        });
      }
      return;
    }
    const data = result.data;
    const lines = [
      allSelected
        ? "本机网站已撤回。"
        : "所选内容已从本机网站撤回。",
    ];
    if (data.audit_event_pending) {
      lines.push("撤回事件待补记，公开内容已生效，请在审计中核对。");
    }
    const pending = target && data.deployment?.status !== "succeeded";
    if (pending) lines.push("线上副本仍可能可见。可先核对线上结果，再同步当前本机网站。");
    renderResult(node, {
      tone: pending || data.audit_event_pending ? "pending" : "ok",
      title: pending ? "本机已撤回，线上待核实" : "撤回成功",
      lines,
      actions: pending
        ? [{ label: "核对线上结果", onClick: () => updateHosting() }]
        : data.active_release_id
        ? [{ label: "查看新版本", onClick: () => openDetail(data.active_release_id) }]
        : [{ label: "刷新", onClick: () => refreshAll(true) }],
    });
    state.withdrawSelected.clear();
    await refreshAll(false);
    renderWithdrawCandidates();
    syncWithdrawButton();
  }

  // ------------------------------------------------------------------
  // Restore
  // ------------------------------------------------------------------
  function populateRestoreSources() {
    const select = byId("restore-source");
    if (!select) return;
    const previous = select.value;
    clearNode(select);
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "选择已离线历史 Release";
    select.appendChild(placeholder);

    const candidates = state.releases
      .filter((record) => record.status === "superseded" || record.status === "withdrawn")
      .sort((a, b) => (Number(b.sequence) || 0) - (Number(a.sequence) || 0));
    candidates.forEach((record) => {
      const option = document.createElement("option");
      option.value = record.release_id;
      const seq = record.sequence === null || record.sequence === undefined ? "—" : record.sequence;
      option.textContent = `#${seq} · ${statusLabel(record.status)} · ${record.release_id} · ${fmtTime(record.created_at)}`;
      select.appendChild(option);
    });
    if (previous && candidates.some((record) => record.release_id === previous)) {
      select.value = previous;
    }
    if (!candidates.length) {
      select.disabled = true;
      const info = byId("restore-source-info");
      if (info) {
        clearNode(info);
        info.appendChild(el("p", "release-note", "没有可作为恢复来源的历史 Release。"));
      }
    } else {
      select.disabled = false;
    }
  }

  async function loadRestoreSource() {
    const select = byId("restore-source");
    const sourceId = select ? select.value : "";
    const info = byId("restore-source-info");
    const candidates = byId("restore-candidates");
    if (!sourceId) {
      showToast("请先选择一个历史 Release。");
      return;
    }
    state.restoreSourceId = sourceId;
    state.restoreExpectedActiveId = state.activeId;
    state.restoreManifest = null;
    state.restoreRecord = null;
    state.restoreSelected.clear();
    state.dryRun = null;
    state.restoreScope = "full";
    byId("restore-scope-full").checked = true;
    byId("restore-scope-subset").checked = false;
    clearNode(candidates);
    clearNode(info);
    info.appendChild(el("p", "release-note", "正在读取来源…"));
    resetResult(byId("restore-preview-result"));
    resetResult(byId("restore-result"));
    const result = await api("GET", `/api/releases/${encodeURIComponent(sourceId)}`);
    if (!result.ok) {
      clearNode(info);
      info.appendChild(el("p", "release-note error", result.error.message || "读取来源失败。"));
      syncRestoreButtons();
      return;
    }
    state.restoreRecord = result.data.record;
    state.restoreManifest = result.data.manifest;
    state.restoreSelected = new Set(
      (state.restoreManifest.content_ids || []).slice()
    );
    renderRestoreSourceInfo();
    renderRestoreCandidates();
    byId("restore-scope-row").classList.remove("hidden");
    syncRestoreButtons();
  }

  function renderRestoreSourceInfo() {
    const info = byId("restore-source-info");
    if (!info) return;
    clearNode(info);
    if (!state.restoreRecord || !state.restoreManifest) return;
    const record = state.restoreRecord;
    const grid = el("div", "release-current-grid");
    grid.appendChild(fact("来源状态", statusLabel(record.status)));
    grid.appendChild(fact("来源操作", actionLabel(actionLabelFromRecord(record))));
    grid.appendChild(fact("内容", String((state.restoreManifest.content_ids || []).length)));
    grid.appendChild(fact("附件", String((state.restoreManifest.attachment_ids || []).length)));
    grid.appendChild(fact("创建时间", fmtTime(record.created_at)));
    info.appendChild(grid);
  }

  function renderRestoreCandidates() {
    const container = byId("restore-candidates");
    if (!container) return;
    clearNode(container);
    if (!state.restoreManifest) return;
    const full = state.restoreScope === "full";
    const items = state.restoreManifest.items || [];
    items.forEach((item) => {
      const card = el("div", "release-pick");
      const head = el("div", "release-pick-head");
      const check = el("label", "release-check");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = full || state.restoreSelected.has(item.content_id);
      checkbox.disabled = full;
      checkbox.setAttribute("aria-label", `选择恢复 ${item.title}`);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) state.restoreSelected.add(item.content_id);
        else state.restoreSelected.delete(item.content_id);
        state.dryRun = null;
        resetResult(byId("restore-preview-result"));
        updateRestoreSummary();
        syncRestoreButtons();
      });
      check.appendChild(checkbox);
      head.appendChild(check);
      const main = el("div", "release-pick-main");
      main.appendChild(el("div", "release-pick-title", item.title || "（无标题）"));
      main.appendChild(
        el(
          "div",
          "release-pick-meta",
          `${item.content_id} · 修订 v${item.revision ?? "—"} · 更新 ${fmtTime(item.updated_at)}`
        )
      );
      const attachmentNames = (item.attachments || [])
        .map((attachment) => attachment.filename)
        .filter(Boolean);
      if (attachmentNames.length) {
        main.appendChild(
          el("div", "release-pick-body", `附件：${attachmentNames.join("、")}`)
        );
      }
      head.appendChild(main);
      card.appendChild(head);
      container.appendChild(card);
    });
    updateRestoreSummary();
    syncRestoreButtons();
  }

  function updateRestoreSummary() {
    const node = byId("restore-summary");
    if (!node) return;
    const total = (state.restoreManifest && state.restoreManifest.content_ids || []).length;
    if (!state.restoreManifest) {
      setText(node, "");
      return;
    }
    if (state.restoreScope === "full") {
      setText(
        node,
        `将全量恢复 ${plural(total, "内容")}及其全部附件。当前公开版本将被新 Release 替代。`
      );
      return;
    }
    const selected = state.restoreSelected.size;
    const allSelected = total > 0 && selected === total;
    setText(
      node,
      allSelected
        ? `已选择全部内容；提交时按全量恢复处理。`
        : `将部分恢复 ${plural(selected, "内容")}；未选择的内容不会公开。`
    );
  }

  function restoreScopeFromSelection() {
    if (state.restoreScope === "full") return null;
    const total = state.restoreManifest ? (state.restoreManifest.content_ids || []).length : 0;
    if (state.restoreSelected.size === total && total > 0) return null;
    return Array.from(state.restoreSelected);
  }

  function syncRestoreButtons() {
    const previewButton = byId("restore-preview-button");
    const submitButton = byId("restore-submit-button");
    if (!previewButton || !submitButton) return;
    const hasSource = Boolean(state.restoreManifest);
    const subsetEmpty = state.restoreScope === "subset" && state.restoreSelected.size === 0;
    previewButton.disabled = state.busy || !hasSource || subsetEmpty;
    const confirmed = byId("restore-final-confirm").checked;
    const withdrawnConfirmed = byId("restore-withdrawn-confirm").checked;
    const needsWithdrawn = Boolean(
      state.dryRun && state.dryRun.requires_confirmation
    );
    submitButton.disabled =
      state.busy ||
      !hasSource ||
      subsetEmpty ||
      !state.dryRun ||
      !confirmed ||
      (needsWithdrawn && !withdrawnConfirmed);
  }

  async function previewRestore() {
    if (state.busy || !state.restoreManifest) return;
    const node = byId("restore-preview-result");
    resetResult(node);
    const contentIds = restoreScopeFromSelection();
    if (state.restoreScope === "subset" && (!contentIds || !contentIds.length)) {
      renderResult(node, { tone: "error", title: "部分恢复必须至少选择一个内容。" });
      return;
    }
    setBusy(true);
    renderResult(node, { tone: "pending", title: "正在执行 dry-run 预览…" });
    const result = await api("POST", "/api/releases/restore", {
      source_release_id: state.restoreSourceId,
      content_ids: contentIds,
      attachment_ids: null,
      expected_active_release_id: state.restoreExpectedActiveId,
      dry_run: true,
    });
    setBusy(false);
    if (!result.ok) {
      state.dryRun = null;
      byId("restore-withdrawn-wrap").classList.add("hidden");
      renderResult(node, {
        tone: "error",
        title: "dry-run 未通过",
        lines: errorLines(result, "恢复预览失败。"),
      });
      syncRestoreButtons();
      return;
    }
    const data = result.data;
    state.dryRun = data;
    const lines = [];
    lines.push(`来源 ${data.source_release_id}；范围 ${data.scope === "full" ? "全量" : "内容子集"}。`);
    lines.push(`将恢复内容 ${(data.content_ids || []).length} 个，附件 ${(data.attachment_ids || []).length} 个。`);
    lines.push(
      data.plan && data.plan.will_supersede_release_id
        ? `当前公开 Release ${data.plan.will_supersede_release_id} 将被替代。`
        : "当前没有公开 Release；恢复后将成为新的公开版本。"
    );
    if (data.already_restored) {
      lines.push("该来源已作为当前公开版本存在，无需再次恢复。");
    }
    if (data.resume_release_id) {
      lines.push(`检测到未激活的恢复 Release ${data.resume_release_id}，提交时将恢复激活该 Release。`);
    }
    (data.warnings || []).forEach((warning) => {
      lines.push(`提示：${warning.detail || warning.code || warning}`);
    });
    const withdrawn = data.previously_withdrawn || {};
    const withdrawnCount = (withdrawn.content_ids || []).length + (withdrawn.attachment_ids || []).length;
    if (withdrawnCount) {
      lines.push(
        `其中 ${withdrawnCount} 项曾在后续版本中被撤回，提交前必须显式确认重新公开。`
      );
    }
    byId("restore-withdrawn-wrap").classList.toggle("hidden", !data.requires_confirmation);
    if (!data.requires_confirmation) {
      byId("restore-withdrawn-confirm").checked = false;
    }
    lines.push("dry-run 只做校验，不生成 Bundle、不改变公开内容。");
    renderResult(node, {
      tone: data.requires_confirmation ? "pending" : "ok",
      title: data.requires_confirmation
        ? "dry-run 完成 · 需要二次确认撤回内容"
        : "dry-run 完成 · 可以提交",
      lines,
    });
    syncRestoreButtons();
  }

  async function submitRestore() {
    if (state.busy || !state.restoreManifest || !state.dryRun) return;
    if (!byId("restore-final-confirm").checked) {
      showToast("请先勾选确认项。");
      return;
    }
    const contentIds = restoreScopeFromSelection();
    const withdrawnConfirm = byId("restore-withdrawn-confirm").checked;
    const total = (state.restoreManifest.content_ids || []).length;
    const count = contentIds === null ? total : contentIds.length;
    const confirmed = await confirmDialog({
      title: "确认历史恢复",
      message:
        `将从 ${state.restoreSourceId} 恢复 ${plural(count, "内容")}，并创建新的公开 Release。` +
        (withdrawnConfirm ? "已确认重新公开曾撤回的内容。" : "若包含曾撤回内容，服务端会再次要求确认。"),
      confirmLabel: "确认执行恢复",
    });
    if (!confirmed) return;

    const node = byId("restore-result");
    resetResult(node);
    setBusy(true);
    renderResult(node, { tone: "pending", title: "正在执行恢复并激活…" });
    const result = await api("POST", "/api/releases/restore", {
      source_release_id: state.restoreSourceId,
      content_ids: contentIds,
      attachment_ids: null,
      expected_active_release_id: state.restoreExpectedActiveId,
      confirm_republish_withdrawn: withdrawnConfirm,
      dry_run: false,
    });
    setBusy(false);

    if (!result.ok) {
      const code = result.error && result.error.code;
      if (result.uncertain) {
        uncertainResult(node, "恢复");
        return;
      }
      if (code === "release_conflict") {
        renderResult(node, {
          tone: "conflict",
          title: "并发冲突 · 当前公开版本已变化",
          lines: [
            "恢复请求基于过期的当前公开状态，服务端拒绝执行。",
            ...errorLines(result, "请刷新后重新预览。"),
          ],
          actions: [{ label: "刷新并重新预览", onClick: () => refreshAll(true) }],
        });
        return;
      }
      if (code === "restore_requires_confirmation") {
        byId("restore-withdrawn-wrap").classList.remove("hidden");
        renderResult(node, {
          tone: "pending",
          title: "需要确认重新公开撤回内容",
          lines: [
            ...errorLines(result, "服务端要求对曾撤回内容进行显式确认。"),
            "请勾选上方确认项，然后再次点击“执行恢复”。",
          ],
        });
        syncRestoreButtons();
        return;
      }
      const details = (result.error && result.error.details) || {};
      if (details.restore_release_id) {
        renderResult(node, {
          tone: "error",
          title: "恢复已生成但激活失败",
          lines: [
            `已生成恢复 Release ${details.restore_release_id}，但未切换公开视图。`,
            ...errorLines(result, "restore 失败。"),
            "不会自动删除该 Release；请刷新历史并审计后决定是否激活。",
          ],
          actions: [
            { label: "查看 Release", onClick: () => openDetail(details.restore_release_id) },
            { label: "刷新", onClick: () => refreshAll(true) },
          ],
        });
        return;
      }
      renderResult(node, {
        tone: "error",
        title: "恢复未完成",
        lines: errorLines(result, "恢复失败。"),
      });
      return;
    }

    const data = result.data;
    const lines = [];
    if (data.already_active || data.already_restored) {
      lines.push(`来源已处于公开状态（Release ${data.active_release_id || state.activeId}），未产生新版本。`);
    } else {
      lines.push(`恢复 Release ${data.release_id || data.active_release_id} 已激活。`);
    }
    if (data.previous_active_release_id) {
      lines.push(`上一公开 Release：${data.previous_active_release_id}`);
    }
    if (data.resumed) {
      lines.push("本次恢复了已准备未激活的恢复 Release，而不是新建版本。");
    }
    if (data.audit_event_pending) {
      lines.push("激活事件待补记：公开内容已生效，请在审计中核对。");
    }
    renderResult(node, {
      tone: data.audit_event_pending ? "pending" : "ok",
      title: data.audit_event_pending ? "恢复成功 · 事件待补记" : "恢复成功",
      lines,
      actions: data.active_release_id
        ? [{ label: "查看 Release", onClick: () => openDetail(data.active_release_id) }]
        : [{ label: "刷新", onClick: () => refreshAll(true) }],
    });
    await refreshAll(false);
  }

  // ------------------------------------------------------------------
  // Detail drawer
  // ------------------------------------------------------------------
  function closeDetail() {
    const drawer = byId("release-drawer");
    const backdrop = byId("release-drawer-backdrop");
    if (drawer) drawer.classList.add("hidden");
    if (backdrop) backdrop.classList.add("hidden");
    state.detailId = null;
  }

  async function openDetail(releaseId) {
    const drawer = byId("release-drawer");
    const backdrop = byId("release-drawer-backdrop");
    const content = byId("release-detail-content");
    if (!drawer || !content) return;
    state.detailId = releaseId;
    setText(byId("release-detail-title"), "Release 详情");
    setText(byId("release-detail-state"), "读取中…");
    clearNode(content);
    content.appendChild(el("p", "release-note", "正在读取 Release 详情…"));
    drawer.classList.remove("hidden");
    backdrop.classList.remove("hidden");

    const result = await api("GET", `/api/releases/${encodeURIComponent(releaseId)}`);
    if (state.detailId !== releaseId) return;
    if (!result.ok) {
      clearNode(content);
      content.appendChild(el("p", "release-note error", result.error.message || "读取 Release 详情失败。"));
      setText(byId("release-detail-state"), "");
      return;
    }
    const record = result.data.record;
    const manifest = result.data.manifest;
    setText(byId("release-detail-title"), shortId(record.release_id));
    renderDetail(record, manifest);
    setText(byId("release-detail-state"), statusLabel(record.status));
  }

  function renderDetail(record, manifest) {
    const content = byId("release-detail-content");
    clearNode(content);

    const basics = el("section", "release-detail-section");
    basics.appendChild(el("h3", null, "基本信息"));
    const kv = el("div", "release-kv");
    addKv(kv, "Release ID", record.release_id, true);
    addKv(kv, "状态", statusLabel(record.status));
    addKv(kv, "操作", actionLabel(actionLabelFromRecord(record)));
    addKv(kv, "序号", record.sequence === null || record.sequence === undefined ? "—" : String(record.sequence));
    addKv(kv, "创建时间", fmtTime(record.created_at));
    addKv(kv, "激活时间", fmtTime(record.activated_at));
    addKv(kv, "被替代时间", fmtTime(record.superseded_at));
    addKv(kv, "撤回时间", fmtTime(record.withdrawn_at));
    addKv(kv, "操作者", record.actor || "owner");
    if (record.reason) addKv(kv, "备注", record.reason);
    if (record.restore_scope) {
      addKv(kv, "恢复范围", record.restore_scope === "full" ? "全量" : "内容子集");
    }
    basics.appendChild(kv);
    content.appendChild(basics);

    const lineage = el("section", "release-detail-section");
    lineage.appendChild(el("h3", null, "来源与后继"));
    const lineageKv = el("div", "release-kv");
    addKv(lineageKv, "父 Release", record.parent_release_id || "—", true);
    addKv(lineageKv, "恢复来源", record.restored_from_release_id || "—", true);
    addKv(lineageKv, "激活来源", record.activated_from_release_id || "—", true);
    addKv(lineageKv, "后继 Release", record.superseded_by_release_id || "—", true);
    const successors = state.releases
      .filter(
        (item) =>
          item.parent_release_id === record.release_id ||
          item.restored_from_release_id === record.release_id
      )
      .map((item) => item.release_id);
    if (successors.length) {
      addKv(lineageKv, "关联版本", successors.join("、"), true);
    }
    lineage.appendChild(lineageKv);
    content.appendChild(lineage);

    const contentsSection = el("section", "release-detail-section");
    contentsSection.appendChild(el("h3", null, "内容与附件"));
    const items = manifest.items || [];
    if (!items.length) {
      contentsSection.appendChild(el("p", "release-note", "该 Release 不包含内容。"));
    } else {
      const list = el("ul", "release-detail-list");
      items.forEach((item) => {
        const row = el("li", "release-detail-item");
        row.appendChild(el("div", "title", item.title || "（无标题）"));
        row.appendChild(
          el(
            "div",
            "meta",
            `${item.content_id} · 修订 v${item.revision ?? "—"} · ${fmtBytes(item.body_bytes)} · 更新 ${fmtTime(item.updated_at)}`
          )
        );
        (item.attachments || []).forEach((attachment) => {
          row.appendChild(
            el(
              "div",
              "meta",
              `附件 ${attachment.filename} · ${fmtBytes(attachment.bytes)} · ${attachment.mime || "unknown"}`
            )
          );
        });
        list.appendChild(row);
      });
      contentsSection.appendChild(list);
    }
    content.appendChild(contentsSection);

    const historySection = el("section", "release-detail-section");
    historySection.appendChild(el("h3", null, "历史操作记录"));
    const historyEntries = Array.isArray(record.history) ? record.history.slice().reverse() : [];
    if (!historyEntries.length) {
      historySection.appendChild(el("p", "release-note", "暂无操作记录。"));
    } else {
      const list = el("ul", "release-detail-list");
      historyEntries.forEach((entry) => {
        const row = el("li", "release-detail-item");
        const action = entry.action || entry.operation || "事件";
        row.appendChild(el("div", "title", actionLabel(action) || action));
        const metaParts = [
          fmtTime(entry.at || entry.created_at),
          entry.actor ? `操作者 ${entry.actor}` : "",
          entry.result || "",
          entry.registry_revision ? `registry v${entry.registry_revision}` : "",
        ].filter(Boolean);
        row.appendChild(el("div", "meta", metaParts.join(" · ")));
        list.appendChild(row);
      });
      historySection.appendChild(list);
    }
    content.appendChild(historySection);

    const integrity = el("section", "release-detail-section");
    integrity.appendChild(el("h3", null, "完整性"));
    const integrityNode = el(
      "div",
      "release-integrity",
      "尚未校验。点击右下角“校验完整性”按需运行 Bundle 文件校验。"
    );
    integrityNode.id = "release-detail-integrity";
    integrity.appendChild(integrityNode);
    content.appendChild(integrity);

    if (record.status === "prepared") {
      const actions = el("div", "release-actions");
      const activate = el("button", "primary-button", "激活并公开此 Release");
      activate.type = "button";
      activate.addEventListener("click", () => activatePrepared(record.release_id));
      actions.appendChild(activate);
      content.appendChild(actions);
      content.appendChild(
        el(
          "p",
          "release-note",
          "该 Release 已准备但未公开。激活会切换公开视图；若只想检查，不必激活。"
        )
      );
    }
  }

  function addKv(container, key, value, mono) {
    container.appendChild(el("div", "release-kv-key", key));
    container.appendChild(
      el("div", `release-kv-value${mono ? " mono" : ""}`, value || "—")
    );
  }

  async function verifyDetail() {
    if (!state.detailId) return;
    const stateNode = byId("release-detail-state");
    setText(stateNode, "正在校验完整性…");
    const result = await api(
      "GET",
      `/api/releases/${encodeURIComponent(state.detailId)}/verify`
    );
    const node = byId("release-detail-integrity");
    if (!node) return;
    if (!result.ok) {
      setText(stateNode, "");
      node.className = "release-integrity error";
      setText(node, result.error.message || "完整性校验失败。");
      return;
    }
    const data = result.data;
    node.className = `release-integrity${data.ok ? "" : " error"}`;
    if (data.ok) {
      setText(
        node,
        `完整性通过：检查 ${data.checked_files ?? "—"} 个文件，Bundle SHA-256 与清单一致。`
      );
    } else {
      const lines = issueLines(data.issues);
      setText(node, `完整性校验未通过：${lines.join("；") || "存在异常文件。"}`);
    }
    setText(stateNode, data.ok ? "完整性通过" : "完整性异常");
  }

  async function activatePrepared(releaseId) {
    const confirmed = await confirmDialog({
      title: "确认激活",
      message: `将把已准备的 Release ${releaseId} 切换为当前公开版本。访客将立即看到其内容。`,
      confirmLabel: "激活并公开",
    });
    if (!confirmed) return;
    setText(byId("release-detail-state"), "正在激活…");
    const result = await api(
      "POST",
      `/api/releases/${encodeURIComponent(releaseId)}/activate`,
      {}
    );
    if (!result.ok) {
      if (result.uncertain) {
        setText(byId("release-detail-state"), "激活结果不确定，请刷新核对。不会自动重试。");
      } else {
        setText(
          byId("release-detail-state"),
          `激活失败：${result.error.message || result.error.code || "未知错误"}`
        );
      }
      return;
    }
    setText(
      byId("release-detail-state"),
      result.data.audit_event_pending ? "已激活 · 事件待补记" : "已激活"
    );
    await refreshAll(false);
    openDetail(releaseId);
  }

  // ------------------------------------------------------------------
  // Tabs and wiring
  // ------------------------------------------------------------------
  function selectTab(name) {
    document.querySelectorAll("[data-release-tab]").forEach((button) => {
      const active = button.getAttribute("data-release-tab") === name;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    ["withdraw", "restore"].forEach((tab) => {
      const panel = byId(`release-tab-${tab}`);
      if (panel) panel.classList.toggle("hidden", tab !== name);
    });
    if (name === "withdraw") renderWithdrawCandidates();
    if (name === "restore") populateRestoreSources();
  }

  function wire() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    state.csrf = meta ? meta.getAttribute("content") || "" : "";

    byId("release-refresh").addEventListener("click", () => refreshAll(true));
    document.querySelectorAll("[data-release-tab]").forEach((button) => {
      button.addEventListener("click", () => selectTab(button.getAttribute("data-release-tab")));
    });

    byId("filter-apply").addEventListener("click", () => loadHistory({ append: false }));
    byId("filter-reset").addEventListener("click", () => {
      byId("filter-status").value = "";
      byId("filter-action").value = "";
      byId("filter-content").value = "";
      byId("filter-since").value = "";
      byId("filter-until").value = "";
      loadHistory({ append: false });
    });
    byId("release-history-more").addEventListener("click", () => loadHistory({ append: true }));

    byId("publish-preview-button").addEventListener("click", previewPublish);
    byId("publish-submit-button").addEventListener("click", publishSelection);
    byId("one-click-publish").addEventListener("click", () => publishOneClick());
    byId("one-click-force").addEventListener("click", () => publishOneClick("local"));
    byId("one-click-redeploy").addEventListener("click", () => publishOneClick("online"));
    byId("hosting-sync").addEventListener("click", syncHostedSnapshot);
    byId("hosting-list").addEventListener("click", async () => {
      if (state.busy || !byId("hosting-account").reportValidity()) return;
      setBusy(true);
      const result = await api("POST", "/api/publish/cloudflare/projects", {
        account: byId("hosting-account").value, token: byId("hosting-token").value,
      });
      const options = byId("hosting-projects");
      clearNode(options);
      if (result.ok) {
        for (const project of result.data.projects) {
          const option = el("option");
          option.value = project;
          options.appendChild(option);
        }
      }
      const projects = result.ok ? result.data.projects : [];
      if (projects.length === 1) byId("hosting-project").value = projects[0];
      setText(byId("hosting-feedback"), result.ok
        ? (projects.length ? "请选择查找到的直接上传项目。" : "此账户没有直接上传项目。")
        : errorLines(result, "无法读取项目。").join(" "));
      setBusy(false);
      if (projects.length) byId("hosting-project").focus();
    });
    byId("hosting-form").addEventListener("submit", (event) => {
      event.preventDefault();
      updateHosting({
        account: byId("hosting-account").value,
        project: byId("hosting-project").value,
        token: byId("hosting-token").value,
      });
    });
    byId("one-click-preview-draft").addEventListener("click", previewDraft);
    byId("withdraw-submit-button").addEventListener("click", submitWithdraw);

    byId("restore-load-button").addEventListener("click", loadRestoreSource);
    byId("restore-source").addEventListener("change", () => {
      state.restoreManifest = null;
      state.restoreSelected.clear();
      state.dryRun = null;
      byId("restore-scope-row").classList.add("hidden");
      clearNode(byId("restore-candidates"));
      clearNode(byId("restore-source-info"));
      resetResult(byId("restore-preview-result"));
      resetResult(byId("restore-result"));
      syncRestoreButtons();
    });
    byId("restore-scope-full").addEventListener("change", () => {
      state.restoreScope = "full";
      state.dryRun = null;
      resetResult(byId("restore-preview-result"));
      renderRestoreCandidates();
    });
    byId("restore-scope-subset").addEventListener("change", () => {
      state.restoreScope = "subset";
      state.dryRun = null;
      resetResult(byId("restore-preview-result"));
      renderRestoreCandidates();
    });
    byId("restore-preview-button").addEventListener("click", previewRestore);
    byId("restore-submit-button").addEventListener("click", submitRestore);
    byId("restore-final-confirm").addEventListener("change", syncRestoreButtons);
    byId("restore-withdrawn-confirm").addEventListener("change", syncRestoreButtons);

    byId("release-detail-close").addEventListener("click", closeDetail);
    byId("release-drawer-backdrop").addEventListener("click", closeDetail);
    byId("release-detail-verify").addEventListener("click", verifyDetail);
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeDetail();
    });

    window.addEventListener("unhandledrejection", () => {
      showToast("发生未预期错误，请刷新后重试。");
    });

    selectTab("publish");
    refreshAll(false);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire, { once: true });
  } else {
    wire();
  }
})();
