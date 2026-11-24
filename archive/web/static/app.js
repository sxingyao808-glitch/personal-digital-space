/* Local archive owner UI. All rendering uses text nodes, never user HTML. */

(() => {
  "use strict";

  const csrfMeta = document.querySelector('meta[name="csrf-token"]');
  const csrf = csrfMeta ? csrfMeta.content : "";

  const $ = (id) => document.getElementById(id);

  const state = {
    items: [],
    current: null,
    isNew: false,
    draft: {
      title: "",
      summary: "",
      body: "",
      visibility: "none",
      type: "THOUGHT",
      primary_space: "ME",
      lifecycle: "DRAFT",
      related_spaces: [],
    },
    revision: null,
    dirty: false,
    saving: false,
    includeDeleted: false,
    query: "",
    space: "HOME",
    typeFilter: "",
    lifecycleFilter: "",
    dataDir: "",
    statusText: "",
    statusClass: "",
    mode: "read",
    overview: null,
    integrity: null,
    drift: null,
    driftError: "",
    publication: null,
    publicationById: new Map(),
    confirmResolve: null,
  };

  const CONTENT_SPACES = ["ME", "BUILD", "THINK", "JOURNEY", "COMPETE"];

  const SHORT_PUBLICATION_LABELS = {
    private: "私有",
    public_not_selected: "未选择",
    selected_not_published: "待发布",
    published: "已发布",
    published_stale: "有新修改",
    withdrawn: "已撤回",
    pending_removal: "需移除",
    deleted: "已删除",
  };

  const SPACE_LABELS = {
    HOME: "入口",
    ME: "关于我",
    BUILD: "创造",
    THINK: "思考",
    JOURNEY: "经历",
    COMPETE: "竞赛",
    ARCHIVE: "归档",
  };

  const SPACE_META = {
    HOME: {
      code: "PERSONAL SPACE",
      name: "入口",
      description:
        "一个只属于你的数字空间。记录创造、思考与经历，让过去自然留下，并在未来重新产生意义。",
    },
    ME: {
      code: "ME",
      name: "关于我",
      description: "我是谁，在意什么，又如何成为现在这样。这里不写简历，只留下真实的自我介绍。",
    },
    BUILD: {
      code: "BUILD",
      name: "创造",
      description: "项目、作品，以及尚未完成的尝试。结果不是唯一的记录对象，过程同样值得保留。",
    },
    THINK: {
      code: "THINK",
      name: "思考",
      description: "想法、判断，以及认知的变化。可以是不成熟的，也可以是后来被自己推翻的。",
    },
    JOURNEY: {
      code: "JOURNEY",
      name: "经历",
      description: "走过的路、做出的选择与转折。它们不一定有结论，但构成了时间里的脉络。",
    },
    COMPETE: {
      code: "COMPETE",
      name: "竞赛",
      description: "比赛、荣誉，以及结果背后的过程。奖项只是一个坐标，不是全部。",
    },
    ARCHIVE: {
      code: "ARCHIVE",
      name: "归档",
      description:
        "已经结束、但仍值得保留的内容。归档按生命周期筛选，不改变内容原来的主要空间，也不等同于删除。",
    },
  };

  const CONTENT_TYPE_LABELS = {
    PROJECT: "项目",
    THOUGHT: "思考",
    EXPERIENCE: "经历",
  };

  const LIFECYCLE_LABELS = {
    DRAFT: "草稿",
    PUBLISHED: "已发布",
    COMPLETED: "已完成",
    ARCHIVED: "已归档",
  };

  const PROVENANCE_LABELS = {
    user: "用户提供",
    ai_inferred: "AI 推断",
    ai_suggested: "AI 建议",
    user_confirmed: "用户确认",
  };

  const STATE_LABELS = {
    PENDING: "写入中",
    READY: "可用",
    BROKEN: "损坏",
    QUARANTINED: "已隔离",
  };

  const TYPE_LABELS = {
    PART_OF: "属于",
    DERIVED_FROM: "衍生自",
    LED_TO: "导致",
    EVOLVED_INTO: "演化为",
    REFERENCES: "引用",
  };

  // ------------------------------------------------------------ utilities

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function formatTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString("zh-CN", { hour12: false });
  }

  function formatBytes(value) {
    if (value === null || value === undefined) return "大小未知";
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / 1024 / 1024).toFixed(2)} MB`;
  }

  function randomToken() {
    if (window.crypto && window.crypto.getRandomValues) {
      const bytes = new Uint8Array(16);
      window.crypto.getRandomValues(bytes);
      return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    }
    return `t${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
  }

  // A retry of the same file must reuse its operation id so the server can
  // resume the same operation/attachment instead of stranding the first
  // attempt. Entries are dropped once the attempt reaches a definite outcome.
  const pendingUploadOperations = new Map();

  function uploadOperationFor(file) {
    const key = `${file.name}:${file.size}:${file.lastModified}`;
    let operationId = pendingUploadOperations.get(key);
    if (!operationId) {
      operationId = `op_${randomToken()}`;
      pendingUploadOperations.set(key, operationId);
    }
    return { key, operationId };
  }

  let toastTimer = null;
  function toast(message, kind = "") {
    const node = $("toast");
    node.textContent = message;
    node.className = `toast${kind ? " " + kind : ""}`;
    if (toastTimer) window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => node.classList.add("hidden"), 4200);
  }

  function confirmDialog(title, message, okLabel = "确认") {
    $("confirm-title").textContent = title;
    $("confirm-message").textContent = message;
    $("confirm-ok").textContent = okLabel;
    const dialog = $("confirm-dialog");
    dialog.showModal();
    return new Promise((resolve) => {
      state.confirmResolve = resolve;
    });
  }

  function settleConfirm(value) {
    const dialog = $("confirm-dialog");
    if (dialog.open) dialog.close();
    const resolve = state.confirmResolve;
    state.confirmResolve = null;
    if (resolve) resolve(value);
  }

  function setStatus(text, className = "") {
    state.statusText = text;
    state.statusClass = className;
    const node = $("save-status");
    node.textContent = text;
    node.className = `save-status${className ? " " + className : ""}`;
  }

  function setUploadStatus(text, className = "") {
    const node = $("upload-status");
    node.textContent = text;
    node.className = `upload-status${className ? " " + className : ""}`;
  }

  // ------------------------------------------------------------ API

  async function api(path, options = {}) {
    const opts = {
      credentials: "same-origin",
      cache: "no-store",
      ...options,
    };
    const headers = { ...(options.headers || {}) };
    let body = options.body;
    if (body && !(body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(body);
    }
    if (opts.method && opts.method !== "GET" && opts.method !== "HEAD") {
      headers["X-CSRF-Token"] = csrf;
    }
    opts.headers = headers;
    opts.body = body;

    let response;
    try {
      response = await fetch(path, opts);
    } catch (error) {
      return {
        status: 0,
        ok: false,
        error: { code: "network_error", message: `无法连接本地服务：${error.message}` },
      };
    }

    if (response.status === 401) {
      window.location.assign("/login");
      return {
        status: 401,
        ok: false,
        error: { code: "unauthorized", message: "登录已失效，请重新进入。" },
      };
    }

    let payload = null;
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }

    if (!payload) {
      return {
        status: response.status,
        ok: false,
        error: {
          code: "bad_response",
          message: `服务返回了无法解析的响应（HTTP ${response.status}）。`,
        },
      };
    }
    return {
      status: response.status,
      ok: response.ok && payload.ok === true,
      data: payload.data,
      error: payload.error,
    };
  }

  function errorText(result) {
    if (!result.error) return `请求失败（HTTP ${result.status}）。`;
    return result.error.message || result.error.code || "请求失败。";
  }

  function formatShortDate(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    const options =
      date.getFullYear() === new Date().getFullYear()
        ? { month: "2-digit", day: "2-digit" }
        : { year: "numeric", month: "2-digit" };
    return date.toLocaleDateString("zh-CN", options);
  }

  // ------------------------------------------------------------ list

  function renderList() {
    const list = $("content-list");
    clear(list);
    const active = state.items.filter((item) => item.state === "active");
    const showSpace = state.space === "HOME";
    for (const item of state.items) {
      const li = el("li");
      const button = el("button", "content-item");
      button.type = "button";
      button.dataset.id = item.id;
      if (state.current && state.current.id === item.id) button.classList.add("active");

      const head = el("span", "item-head");
      const title = el("span", "item-title");
      const fullTitle = (item.title || "").trim();
      title.textContent = fullTitle || "未命名";
      title.title = fullTitle || "未命名";
      if (!fullTitle) title.classList.add("untitled");
      head.append(title);
      const date = formatShortDate(item.updated_at);
      if (date) head.append(el("span", "item-date", date));

      const excerpt = el("span", "item-excerpt");
      const fullExcerpt = item.excerpt || "空白记录";
      excerpt.textContent = fullExcerpt;
      excerpt.title = fullExcerpt;

      const meta = el("span", "item-meta");
      const taxonomy = [];
      if (showSpace && item.primary_space) {
        taxonomy.push(SPACE_LABELS[item.primary_space] || item.primary_space);
      }
      if (item.type) taxonomy.push(CONTENT_TYPE_LABELS[item.type] || item.type);
      if (taxonomy.length) {
        meta.append(el("span", "item-taxonomy", taxonomy.join(" · ")));
      }
      if (item.state === "deleted") {
        meta.append(el("span", "mini-flag deleted", "回收站"));
      } else if (state.publicationById.has(item.id)) {
        const publication = state.publicationById.get(item.id);
        meta.append(
          el(
            "span",
            `mini-flag pub-${publication.state}`,
            SHORT_PUBLICATION_LABELS[publication.state] || publication.label
          )
        );
      } else if (item.public) {
        meta.append(el("span", "mini-flag public", "公开"));
      }
      if (item.relation_count) meta.append(el("span", "item-count", `关系 ${item.relation_count}`));
      if (item.attachment_count) {
        meta.append(el("span", "item-count", `附件 ${item.attachment_count}`));
      }

      button.append(head, excerpt, meta);
      button.addEventListener("click", () => selectContent(item.id));
      li.append(button);
      list.append(li);
    }
    renderListEmpty();
    const scopeLabel = state.space === "HOME" ? "全部内容" : SPACE_LABELS[state.space] || state.space;
    $("list-label").textContent = state.includeDeleted
      ? `回收站 · ${active.length} 条有效`
      : `${scopeLabel} · ${active.length} 条`;
  }

  function renderListEmpty() {
    const node = $("list-empty");
    const action = $("list-empty-action");
    if (state.items.length) {
      node.classList.add("hidden");
      return;
    }
    node.classList.remove("hidden");
    action.classList.add("hidden");
    action.onclick = null;
    const text = $("list-empty-text");

    if (state.includeDeleted) {
      text.textContent = "回收站是空的。删除的内容会保留在这里，可随时恢复。";
      return;
    }
    if (state.query) {
      text.textContent = `没有找到与「${state.query}」相关的内容。`;
      action.textContent = "清除搜索";
      action.classList.remove("hidden");
      action.onclick = () => {
        $("search-input").value = "";
        state.query = "";
        loadList();
      };
      return;
    }
    if (state.typeFilter || state.lifecycleFilter) {
      text.textContent = "当前筛选下没有内容。";
      action.textContent = "清除筛选";
      action.classList.remove("hidden");
      action.onclick = () => {
        state.typeFilter = "";
        state.lifecycleFilter = "";
        $("type-filter").value = "";
        $("lifecycle-filter").value = "";
        loadList();
      };
      return;
    }
    if (state.space === "ARCHIVE") {
      text.textContent = "还没有归档的内容。把一条内容的状态设为「已归档」，它会出现在这里。";
      return;
    }
    if (CONTENT_SPACES.includes(state.space)) {
      const name = SPACE_LABELS[state.space];
      text.textContent = `${name}还没有内容。`;
      action.textContent = `在${name}新建`;
      action.classList.remove("hidden");
      action.onclick = () => newDraft();
      return;
    }
    text.textContent = "这里还没有记录。";
    action.textContent = "写下第一条";
    action.classList.remove("hidden");
    action.onclick = () => newDraft();
  }

  async function loadPublicationState() {
    const result = await api("/api/publication/state");
    state.publication = result.ok ? result.data : null;
    state.publicationById = new Map();
    if (result.ok) {
      for (const entry of result.data.contents || []) {
        state.publicationById.set(entry.content_id, entry);
      }
    }
    return result.ok;
  }

  async function loadList() {
    const params = new URLSearchParams();
    if (state.query) params.set("q", state.query);
    if (state.includeDeleted) params.set("deleted", "1");
    if (state.space !== "HOME" && state.space !== "ARCHIVE") {
      params.set("space", state.space);
    }
    const lifecycle = state.space === "ARCHIVE"
      ? state.lifecycleFilter || "ARCHIVED"
      : state.lifecycleFilter;
    if (lifecycle) params.set("lifecycle", lifecycle);
    if (state.typeFilter) params.set("type", state.typeFilter);
    const result = await api(`/api/content?${params.toString()}`);
    if (!result.ok) {
      toast(`列表载入失败：${errorText(result)}`, "error");
      return;
    }
    await loadPublicationState();
    state.items = result.data.items;
    renderList();
    if (!state.current && !state.isNew) renderMainPanel();
  }

  // ------------------------------------------------------------ editor

  function showEmpty() {
    document.querySelector(".layout").classList.remove("detail-open");
    $("overview-panel").classList.add("hidden");
    $("empty-state").classList.remove("hidden");
    $("editor-panel").classList.add("hidden");
    $("conflict-banner").classList.add("hidden");
  }

  function showOverview() {
    document.querySelector(".layout").classList.remove("detail-open");
    $("empty-state").classList.add("hidden");
    $("overview-panel").classList.remove("hidden");
    $("editor-panel").classList.add("hidden");
    $("conflict-banner").classList.add("hidden");
    renderOverview();
  }

  function renderMainPanel() {
    if (state.overview && state.overview.total > 0) {
      showOverview();
    } else {
      showEmpty();
    }
  }

  async function refreshOverview() {
    const result = await api("/api/overview");
    if (!result.ok) return;
    state.overview = result.data;
    if (!state.current && !state.isNew) renderMainPanel();
  }

  function recentForPanel() {
    if (state.space === "HOME") {
      return (state.overview && state.overview.recent) || [];
    }
    return state.items.slice(0, 5);
  }

  function renderOverview() {
    const space = state.space;
    const meta = SPACE_META[space] || SPACE_META.HOME;
    const overview = state.overview;
    const isHome = space === "HOME";
    $("overview-eyebrow").textContent = isHome ? meta.code : `${meta.code} / SPACE`;
    $("overview-title").textContent = isHome ? "个人数字空间" : meta.name;
    $("overview-copy").textContent = meta.description;
    $("overview-new").textContent = isHome
      ? "新建内容"
      : `在${meta.name}新建`;
    $("overview-recent-label").textContent = isHome ? "最近更新" : `${meta.name}最近更新`;

    const cards = $("overview-spaces");
    clear(cards);
    cards.classList.toggle("hidden", !isHome);
    if (isHome && overview) {
      for (const code of [...CONTENT_SPACES, "ARCHIVE"]) {
        const info = SPACE_META[code];
        const count = code === "ARCHIVE" ? overview.archived : overview.spaces[code] || 0;
        const card = el("button", "space-card");
        card.type = "button";
        card.dataset.space = code;
        const top = el("span", "space-card-top");
        top.append(
          el("span", "space-card-code", info.code),
          el("span", "space-card-count", `${count}`)
        );
        card.append(
          top,
          el("span", "space-card-name", info.name),
          el("span", "space-card-desc", info.description)
        );
        card.addEventListener("click", () => chooseSpace(code));
        cards.append(card);
      }
    }

    const list = $("overview-recent-list");
    clear(list);
    const recent = recentForPanel();
    for (const item of recent) {
      const li = el("li");
      const button = el("button", "overview-item");
      button.type = "button";
      const title = el("span", "overview-item-title", item.title || "未命名");
      const metaLine = el("span", "overview-item-meta");
      const bits = [
        SPACE_LABELS[item.primary_space] || item.primary_space,
        CONTENT_TYPE_LABELS[item.type] || item.type,
        formatShortDate(item.updated_at),
      ].filter(Boolean);
      metaLine.textContent = bits.join(" · ");
      button.append(title, metaLine);
      button.addEventListener("click", () => selectContent(item.id));
      li.append(button);
      list.append(li);
    }
    const empty = $("overview-recent-empty");
    empty.classList.toggle("hidden", recent.length > 0);
    if (!recent.length) {
      empty.textContent = isHome
        ? "这里还没有记录。"
        : `${meta.name}还没有内容，可以在这里写下第一条。`;
    }
  }

  function chooseSpace(space) {
    if (state.current || state.isNew) {
      closeContent();
      if (state.current || state.isNew) return;
    }
    state.space = space;
    for (const button of document.querySelectorAll(".space-button")) {
      const active = button.dataset.space === space;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    }
    loadList();
  }

  function closeContent() {
    if (state.dirty && !window.confirm("有未保存的修改，确定返回吗？")) return;
    state.current = null;
    state.isNew = false;
    state.dirty = false;
    state.statusText = "";
    state.statusClass = "";
    state.mode = "read";
    renderList();
    renderMainPanel();
  }

  function showEditor() {
    document.querySelector(".layout").classList.add("detail-open");
    $("empty-state").classList.add("hidden");
    $("overview-panel").classList.add("hidden");
    $("editor-panel").classList.remove("hidden");
    if (window.matchMedia("(max-width: 900px)").matches) {
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
  }

  function isReadonly() {
    return Boolean(state.current && state.current.state === "deleted");
  }

  function syncDraftFromCurrent() {
    state.draft = {
      title: state.current.title,
      summary: state.current.summary || "",
      body: state.current.body,
      visibility: state.current.visibility_selected ? state.current.visibility : "none",
      type: state.current.type || "THOUGHT",
      primary_space: state.current.primary_space || "ME",
      lifecycle: state.current.lifecycle || "DRAFT",
      related_spaces: [...(state.current.related_spaces || [])],
    };
  }

  function renderEditor(syncInputs = false) {
    const current = state.current;
    if (!current && !state.isNew) {
      renderMainPanel();
      return;
    }
    showEditor();
    if (state.isNew) state.mode = "edit";
    const readonly = isReadonly();
    const editing = state.mode === "edit" && !readonly;
    $("reader-view").classList.toggle("hidden", editing);
    $("edit-view").classList.toggle("hidden", !editing);
    $("edit-button").classList.toggle("hidden", editing || readonly || state.isNew);
    $("editor-panel").classList.toggle("mode-read", !editing);

    $("content-id").textContent = state.isNew ? "尚未保存" : current.id;
    $("content-state").className = "state-chip";
    let stateText = "草稿";
    if (state.isNew) {
      stateText = "草稿";
    } else if (current.state === "deleted") {
      stateText = "已删除 · 可恢复";
      $("content-state").classList.add("deleted");
    } else if (current.public) {
      stateText = "公开";
      $("content-state").classList.add("public");
    } else if (!current.visibility_selected) {
      stateText = "未选择 · 按私有";
    } else {
      stateText = "私有";
    }
    $("content-state").textContent = stateText;

    const publicLink = $("public-link");
    publicLink.classList.toggle("hidden", !(current && current.public));
    if (current && current.public) publicLink.href = `/p/${current.id}`;

    $("delete-button").classList.toggle("hidden", state.isNew || readonly);
    $("restore-button").classList.toggle("hidden", !readonly);
    const banner = $("readonly-banner");
    banner.classList.toggle("hidden", !readonly);
    banner.textContent = readonly
      ? "这条内容在回收站中，只能查看或恢复。恢复会保留身份、关系与属性；未就绪的附件会如实列出。"
      : "";

    if (syncInputs) {
      $("title-input").value = state.draft.title;
      $("summary-input").value = state.draft.summary;
      $("body-input").value = state.draft.body;
      $("type-input").value = state.draft.type;
      $("primary-space-input").value = state.draft.primary_space;
      $("lifecycle-input").value = state.draft.lifecycle;
    }
    $("title-input").disabled = readonly;
    $("summary-input").disabled = readonly;
    $("body-input").disabled = readonly;
    $("type-input").disabled = readonly;
    $("primary-space-input").disabled = readonly;
    $("lifecycle-input").disabled = readonly;
    renderRelatedSpaces(readonly);
    $("save-button").disabled = readonly || state.saving;
    $("save-button").textContent = state.isNew ? "保存" : "保存";

    for (const button of document.querySelectorAll(".visibility-button")) {
      button.disabled = readonly;
      button.classList.toggle("active", button.dataset.visibility === state.draft.visibility);
    }

    if (!state.saving && !state.statusText) {
      setStatus(state.isNew ? "尚未保存" : `版本 ${current.revision}`, "");
    } else {
      setStatus(state.statusText, state.statusClass);
    }

    if (!editing) renderReader();
    renderRelations();
    renderAttributes();
    renderAttachments();
    renderInfo();
    renderDriftNote();
  }

  function renderReader() {
    const current = state.current;
    if (!current) return;
    const metaParts = [
      `${SPACE_LABELS[current.primary_space] || current.primary_space}`,
      CONTENT_TYPE_LABELS[current.type] || current.type,
      LIFECYCLE_LABELS[current.lifecycle] || current.lifecycle,
      `更新于 ${formatTime(current.updated_at)}`,
    ];
    if (current.related_spaces && current.related_spaces.length) {
      metaParts.push(
        `另见于 ${current.related_spaces.map((s) => SPACE_LABELS[s] || s).join("、")}`
      );
    }
    $("reader-meta").textContent = metaParts.filter(Boolean).join(" · ");

    const title = (state.draft.title || "").trim();
    $("reader-title").textContent = title || "未命名";
    $("reader-title").classList.toggle("untitled", !title);

    const summary = (state.draft.summary || "").trim();
    $("reader-summary").textContent = summary;
    $("reader-summary").classList.toggle("hidden", !summary);

    $("reader-body").textContent = state.draft.body && state.draft.body.trim()
      ? state.draft.body
      : "这条内容还没有正文。";
  }

  function markDirty() {
    state.dirty = true;
    $("conflict-banner").classList.add("hidden");
    setStatus("有未保存的修改", "");
  }

  function renderRelatedSpaces(readonly) {
    const container = $("related-space-options");
    clear(container);
    for (const space of CONTENT_SPACES) {
      const label = el("label", "space-option");
      const box = el("input");
      box.type = "checkbox";
      box.value = space;
      box.checked =
        state.draft.related_spaces.includes(space) &&
        space !== state.draft.primary_space;
      box.disabled = Boolean(readonly) || space === state.draft.primary_space;
      box.addEventListener("change", () => {
        const next = new Set(state.draft.related_spaces);
        if (box.checked) next.add(space);
        else next.delete(space);
        state.draft.related_spaces = [...next];
        markDirty();
      });
      label.append(
        box,
        document.createTextNode(`${space} · ${SPACE_LABELS[space] || space}`)
      );
      container.append(label);
    }
  }

  // ---------------------------------------------------- publication drift

  const DRIFT_HINT = {
    PUBLISHED_AND_CURRENT: { tone: "ok", text: "公开副本与当前内容一致。" },
    PUBLISHED_SNAPSHOT_STALE: {
      tone: "warn",
      text: "公开页面仍是上一次发布的快照；此后的编辑尚未公开。",
    },
    PUBLISHED_BUT_NOW_PRIVATE: {
      tone: "risk",
      text: "本机网站仍有公开副本，内容在资料库里已是私有。",
    },
    PUBLISHED_BUT_DELETED: {
      tone: "risk",
      text: "本机网站仍有公开副本，内容已移入回收站。",
    },
    PUBLISHED_BUT_DESELECTED: {
      tone: "risk",
      text: "本机网站仍有公开副本，内容已取消选择公开。",
    },
    SELECTED_BUT_NOT_PUBLISHED: {
      tone: "warn",
      text: "已选择公开，但尚未进入当前生效的发布版本。",
    },
    NOT_IN_ACTIVE_RELEASE: {
      tone: "info",
      text: "尚未进入任何公开版本。",
    },
  };

  async function loadDrift() {
    const result = await api("/api/publication/drift");
    state.drift = result.ok ? result.data : null;
    state.driftError = result.ok ? "" : errorText(result);
    if (state.current && !state.isNew) renderDriftNote();
  }

  function renderDriftNote() {
    const node = $("publication-drift-note");
    if (!node) return;
    clear(node);
    const current = state.current;
    const publication = state.publicationById.get(current ? current.id : "");
    if (!current || state.isNew || (!state.drift && !publication)) {
      node.className = "drift-note hidden";
      return;
    }
    const report = state.drift || { contents: [], attachments: [] };
    const hints = [];
    if (publication) {
      const tone =
        publication.state === "pending_removal" || publication.state === "deleted"
          ? "risk"
          : publication.state === "published"
            ? "ok"
            : publication.state === "private"
              ? ""
              : "warn";
      hints.push({ tone, text: `发布状态：${publication.label}` });
    }
    if (publication?.online_removal_pending) {
      hints.push({ tone: "risk", text: "旧的线上公开副本仍可能被访问，请确认从公开网站移除。" });
    }
    if (report.state === "uncertain") {
      hints.push({
        tone: "warn",
        text: "无法完整核对当前公开副本，差异结论不确定。请到发布管理页查看详情。",
      });
    }
    const rows = (report.contents || []).filter((row) => row.content_id === current.id);
    const attachmentIds = new Set((current.attachments || []).map((item) => item.id));
    const driftAttachments = (report.attachments || []).filter(
      (item) => item.content_id === current.id || attachmentIds.has(item.attachment_id)
    );
    for (const row of rows) {
      const config = DRIFT_HINT[row.category];
      if (config) hints.push({ ...config, detail: row.detail });
    }
    if (driftAttachments.length) {
      hints.push({
        tone: "risk",
        text: `附件状态与已发布副本不一致（${driftAttachments.length} 个）。`,
        detail: driftAttachments
          .map(
            (item) =>
              `${item.published_filename || item.attachment_id}: ${(item.reasons || []).join("；")}`
          )
          .join(" / "),
      });
    }
    if (!hints.length) {
      node.className = "drift-note hidden";
      return;
    }
    const tone = hints.some((hint) => hint.tone === "risk")
      ? "risk"
      : hints.some((hint) => hint.tone === "warn")
        ? "warn"
        : "ok";
    node.className = `drift-note ${tone}`;
    const title = el("div", "drift-note-title", "发布状态（来自发布记录）");
    node.append(title);
    for (const hint of hints) {
      const line = el(
        "div",
        `drift-note-line ${hint.tone}`,
        hint.detail ? `${hint.text} ${hint.detail}` : hint.text
      );
      node.append(line);
    }
    const removal = publication?.state === "pending_removal";
    const link = el("a", "drift-note-link", removal ? "从公开网站移除 →" : "查看发布状态 →");
    link.href = removal ? "/releases#one-click-title" : "/releases";
    node.append(link);
  }

  async function selectContent(contentId) {
    if (state.dirty && !state.isNew) {
      const proceed = await confirmDialog(
        "放弃未保存的修改？",
        "当前输入尚未写入。切换到其他内容会丢弃这些修改。",
        "放弃修改"
      );
      if (!proceed) return;
    }
    const suffix = state.includeDeleted ? "?include_deleted=1" : "";
    const result = await api(`/api/content/${contentId}${suffix}`);
    if (!result.ok) {
      toast(`载入失败：${errorText(result)}`, "error");
      return;
    }
    state.current = result.data;
    state.isNew = false;
    state.revision = result.data.revision;
    state.dirty = false;
    state.statusText = "";
    state.statusClass = "";
    state.mode = "read";
    $("conflict-banner").classList.add("hidden");
    syncDraftFromCurrent();
    renderEditor(true);
    renderList();
  }

  async function reloadCurrent() {
    if (!state.current || state.isNew) return;
    const suffix = state.includeDeleted ? "?include_deleted=1" : "";
    const result = await api(`/api/content/${state.current.id}${suffix}`);
    if (!result.ok) return;
    state.current = result.data;
    state.revision = result.data.revision;
    if (!state.dirty) {
      syncDraftFromCurrent();
      renderEditor(true);
    } else {
      renderEditor(false);
    }
  }

  function newDraft() {
    state.current = null;
    state.isNew = true;
    state.revision = null;
    state.dirty = false;
    state.statusText = "";
    state.statusClass = "";
    state.mode = "edit";
    const defaultSpace = CONTENT_SPACES.includes(state.space) ? state.space : "ME";
    state.draft = {
      title: "",
      summary: "",
      body: "",
      visibility: "none",
      type: "THOUGHT",
      primary_space: defaultSpace,
      lifecycle: "DRAFT",
      related_spaces: [],
    };
    $("conflict-banner").classList.add("hidden");
    renderEditor(true);
    renderList();
    $("title-input").focus();
  }

  async function saveCurrent() {
    if (state.saving || isReadonly()) return;
    if (!state.isNew && !state.dirty) {
      setStatus(`版本 ${state.revision} · 无待保存修改`, "");
      return;
    }
    state.saving = true;
    $("save-button").disabled = true;
    $("conflict-banner").classList.add("hidden");
    setStatus("正在保存…", "");

    const visibility = state.draft.visibility === "none" ? null : state.draft.visibility;
    const payload = {
      title: state.draft.title,
      summary: state.draft.summary,
      body: state.draft.body,
      visibility,
      type: state.draft.type,
      primary_space: state.draft.primary_space,
      lifecycle: state.draft.lifecycle,
      related_spaces: state.draft.related_spaces,
    };

    let result;
    if (state.isNew) {
      result = await api("/api/content", { method: "POST", body: payload });
    } else {
      result = await api(`/api/content/${state.current.id}`, {
        method: "PATCH",
        body: { ...payload, revision: state.revision },
      });
    }

    state.saving = false;
    $("save-button").disabled = false;

    if (result.ok) {
      state.current = result.data;
      state.isNew = false;
      state.revision = result.data.revision;
      state.dirty = false;
      state.mode = "read";
      syncDraftFromCurrent();
      renderEditor(true);
      setStatus(`已保存 · 版本 ${result.data.revision}`, "ok");
      toast("已保存");
      await loadList();
      await refreshIntegrityBadge();
      await loadDrift();
      await refreshOverview();
      return;
    }

    if (result.status === 409) {
      const details = result.error && result.error.details ? result.error.details : {};
      const suffix = details.current_revision
        ? `服务器当前版本 ${details.current_revision}。你的输入仍保留。`
        : "你的输入仍保留。";
      $("conflict-text").textContent = `内容已在别处更新，本次保存未写入。${suffix}`;
      $("conflict-banner").classList.remove("hidden");
      setStatus("保存未完成：内容已在别处更新", "error");
      return;
    }

    if (result.status === 202) {
      setStatus("结果不确定：服务未确认写入结果", "error");
      toast("保存结果不确定。请稍后重新载入确认，或检查完整性。", "error");
      return;
    }

    setStatus(`保存失败：${errorText(result)}`, "error");
    toast(`保存失败：${errorText(result)}`, "error");
  }

  async function deleteCurrent() {
    if (state.isNew || !state.current || isReadonly()) return;
    const relations = state.current.relations.length;
    const attributes = state.current.attributes.length;
    const attachments = state.current.attachments.length;
    const published = state.publicationById.get(state.current.id);
    const remains = published?.published_revision != null || published?.online_copy_possible;
    const proceed = await confirmDialog(
      "删除这条内容？",
      `内容将移入回收站，身份与原始内容保留，可随时恢复。\n关联关系 ${relations} 条、属性 ${attributes} 条、附件 ${attachments} 个不会被级联删除。` +
        (remains ? "\n公开副本仍可能被访问。删除后可前往发布管理移除。" : ""),
      "删除"
    );
    if (!proceed) return;

    const result = await api(`/api/content/${state.current.id}`, {
      method: "DELETE",
      body: { revision: state.revision },
    });
    if (!result.ok) {
      if (result.status === 409) {
        setStatus("删除未执行：内容已在别处更新", "error");
        toast("删除未执行，内容版本已变化。请重新载入后再试。", "error");
      } else {
        toast(`删除失败：${errorText(result)}`, "error");
      }
      return;
    }
    state.current = result.data.content;
    state.revision = result.data.content.revision;
    state.dirty = false;
    state.mode = "read";
    syncDraftFromCurrent();
    renderEditor(true);
    await loadList();
    await refreshOverview();
    await loadDrift();
    toast(remains ? "已移入回收站。请确认移除公开副本。" : "已移入回收站。");
  }

  async function restoreCurrent() {
    if (!state.current || !isReadonly()) return;
    const result = await api(`/api/content/${state.current.id}/restore`, { method: "POST" });
    if (!result.ok) {
      toast(`恢复失败：${errorText(result)}`, "error");
      return;
    }
    state.current = result.data.content;
    state.revision = result.data.content.revision;
    state.dirty = false;
    state.mode = "read";
    syncDraftFromCurrent();
    renderEditor(true);
    await loadList();
    await refreshOverview();
    const report = result.data.report;
    if (report.complete) {
      toast(`已恢复，身份与 ${report.relations} 条关系、${report.attributes} 条属性一并保留。`);
    } else {
      toast(
        `已恢复，但 ${report.attachments_not_ready.length} 个附件尚未就绪。完整性核对会继续报告这些附件。`,
        "error"
      );
    }
  }

  // ------------------------------------------------------------ relations

  function renderRelations() {
    const list = $("relations-list");
    clear(list);
    const current = state.current;
    const relations = current ? current.relations : [];
    for (const relation of relations) {
      const item = el("li", "record");
      if (!relation.other_resolvable) item.classList.add("broken");
      const main = el("div", "record-main");
      const title = el("div", "record-title");
      const direction = relation.direction === "outgoing" ? "→" : "←";
      const target = el(
        "button",
        "relation-link",
        relation.other_title || relation.other_id || "未知端点"
      );
      target.type = "button";
      target.disabled = !relation.other_resolvable;
      if (relation.other_resolvable) {
        target.title = `打开 ${relation.other_title || relation.other_id}`;
        target.addEventListener("click", () => selectContent(relation.other_id));
      }
      title.append(
        el("span", "relation-type", relation.type),
        document.createTextNode(` ${direction} `),
        target
      );
      main.append(title);
      const sub = el("div", "record-sub");
      const parts = [
        relation.direction === "outgoing" ? "出向" : "入向",
        TYPE_LABELS[relation.type] || relation.type,
        relation.other_id,
      ];
      sub.textContent = parts.join(" · ");
      if (!relation.other_resolvable) {
        sub.textContent += " · 端点不可用（已删除或缺失）";
      }
      main.append(sub);
      const actions = el("div", "record-actions");
      const remove = el("button", "ghost-button tiny danger", "移除");
      remove.type = "button";
      remove.addEventListener("click", () => removeRelation(relation));
      actions.append(remove);
      item.append(main, actions);
      list.append(item);
    }
    $("relations-empty").classList.toggle("hidden", relations.length > 0);

    const select = $("relation-target");
    const previous = select.value;
    clear(select);
    const choices = state.items.filter(
      (item) => item.state === "active" && (!current || item.id !== current.id)
    );
    if (!choices.length) {
      const option = el(
        "option",
        "",
        "当前列表中没有其他内容（清除搜索或筛选后可选更多）"
      );
      option.value = "";
      select.append(option);
      select.disabled = true;
    } else {
      select.disabled = false;
      for (const item of choices) {
        const option = el("option", "", item.title || `未命名 · ${item.id}`);
        option.value = item.id;
        select.append(option);
      }
      if (choices.some((item) => item.id === previous)) select.value = previous;
    }
    $("add-relation-button").disabled = select.disabled || isReadonly();
  }

  async function addRelation() {
    if (!state.current || isReadonly()) return;
    const target = $("relation-target").value;
    const type = $("relation-type").value;
    if (!target) {
      toast("请选择关系目标。", "error");
      return;
    }
    const result = await api(`/api/content/${state.current.id}/relations`, {
      method: "POST",
      body: { to_id: target, type },
    });
    if (!result.ok) {
      toast(`关系未创建：${errorText(result)}`, "error");
      return;
    }
    await reloadCurrent();
    toast("关系已记录。");
  }

  async function removeRelation(relation) {
    const proceed = await confirmDialog(
      "移除这条关系？",
      `${relation.type} · ${relation.other_title || relation.other_id}\n只移除连接本身，不会删除任一端的内容。`,
      "移除"
    );
    if (!proceed) return;
    const result = await api(`/api/relations/${relation.id}`, { method: "DELETE" });
    if (!result.ok) {
      toast(`关系未移除：${errorText(result)}`, "error");
      return;
    }
    await reloadCurrent();
    toast("关系已移除。");
  }

  // ------------------------------------------------------------ attributes

  function renderAttributes() {
    const list = $("attributes-list");
    clear(list);
    const attributes = state.current ? state.current.attributes : [];
    for (const attribute of attributes) {
      const item = el("li", "record");
      const main = el("div", "record-main");
      const title = el("div", "record-title");
      title.textContent = `${attribute.key}: ${attribute.value}`;
      main.append(title);

      const sub = el("div", "record-sub");
      const provenance = el(
        "span",
        attribute.provenance.startsWith("ai_") ? "tag ai" : "tag",
        PROVENANCE_LABELS[attribute.provenance] || attribute.provenance
      );
      const confirmed = el(
        "span",
        attribute.confirmed ? "tag confirmed" : "tag",
        attribute.confirmed ? "已确认" : "未确认"
      );
      sub.append(provenance, document.createTextNode(" "), confirmed);
      if (attribute.source_ref) sub.append(document.createTextNode(` · ${attribute.source_ref}`));
      main.append(sub);

      const actions = el("div", "record-actions");
      if (!attribute.confirmed && !isReadonly()) {
        const confirm = el("button", "ghost-button tiny", "确认");
        confirm.type = "button";
        confirm.title = "确认后会标记为“用户确认”。";
        confirm.addEventListener("click", () => confirmAttribute(attribute));
        actions.append(confirm);
      }
      item.append(main, actions);
      list.append(item);
    }
    $("attributes-empty").classList.toggle("hidden", attributes.length > 0);
    $("add-attribute-button").disabled = isReadonly();
  }

  async function addAttribute() {
    if (!state.current || isReadonly()) return;
    const key = $("attribute-key").value.trim();
    const value = $("attribute-value").value.trim();
    if (!key || !value) {
      toast("属性名称和值都不能为空。", "error");
      return;
    }
    const result = await api(`/api/content/${state.current.id}/attributes`, {
      method: "POST",
      body: {
        key,
        value,
        provenance: $("attribute-provenance").value,
        confirmed: $("attribute-confirmed").checked,
        source_ref: $("attribute-source").value.trim() || null,
      },
    });
    if (!result.ok) {
      toast(`属性未添加：${errorText(result)}`, "error");
      return;
    }
    $("attribute-key").value = "";
    $("attribute-value").value = "";
    $("attribute-source").value = "";
    $("attribute-confirmed").checked = false;
    await reloadCurrent();
    toast("属性已添加。");
  }

  async function confirmAttribute(attribute) {
    const result = await api(`/api/attributes/${attribute.id}`, {
      method: "PATCH",
      body: { confirmed: true },
    });
    if (!result.ok) {
      toast(`属性未确认：${errorText(result)}`, "error");
      return;
    }
    await reloadCurrent();
    toast("属性已标记为用户确认。");
  }

  // ------------------------------------------------------------ attachments

  function renderAttachments() {
    const list = $("attachments-list");
    clear(list);
    const attachments = state.current ? state.current.attachments : [];
    for (const attachment of attachments) {
      const item = el("li", "record");
      if (attachment.state !== "READY") item.classList.add("broken");
      const main = el("div", "record-main");
      const title = el("div", "record-title");
      title.textContent = attachment.filename;
      main.append(title);

      const sub = el("div", "record-sub");
      const stateTag = el(
        "span",
        attachment.state === "READY" ? "tag" : "tag broken",
        STATE_LABELS[attachment.state] || attachment.state
      );
      const visibility = el(
        "span",
        attachment.visibility === "public" ? "tag" : "tag",
        attachment.visibility === "public" ? "公开附件" : "私有附件"
      );
      sub.append(stateTag, document.createTextNode(" "), visibility);
      sub.append(
        document.createTextNode(
          ` · ${formatBytes(attachment.byte_size)} · ${attachment.sha256 ? attachment.sha256.slice(0, 12) : "无哈希"}`
        )
      );
      main.append(sub);
      if (attachment.note) {
        const note = el("div", "record-sub", attachment.note);
        main.append(note);
      }

      const actions = el("div", "record-actions");
      if (attachment.state === "READY") {
        const link = el("a", "ghost-button tiny", "下载");
        link.href = `/api/attachments/${attachment.id}/file`;
        link.rel = "noopener";
        actions.append(link);
      }
      if (!isReadonly()) {
        const toggle = el(
          "button",
          "ghost-button tiny",
          attachment.visibility === "public" ? "设为私有" : "设为公开"
        );
        toggle.type = "button";
        toggle.addEventListener("click", () => toggleAttachment(attachment));
        actions.append(toggle);
      }
      item.append(main, actions);
      list.append(item);
    }
    $("attachments-empty").classList.toggle("hidden", attachments.length > 0);
    $("attachment-input").disabled = isReadonly();
  }

  async function uploadAttachment(file) {
    if (!state.current || state.isNew || isReadonly()) return;
    setUploadStatus("正在写入并核对…", "");
    $("attachment-input").disabled = true;

    const pending = uploadOperationFor(file);
    const form = new FormData();
    form.append("file", file);
    form.append("operation_id", pending.operationId);
    form.append("expected_size", String(file.size));

    const result = await api(`/api/content/${state.current.id}/attachments`, {
      method: "POST",
      body: form,
    });

    $("attachment-input").disabled = false;
    $("attachment-input").value = "";

    if (result.ok) {
      pendingUploadOperations.delete(pending.key);
      setUploadStatus("已写入并核对哈希。", "ok");
      await reloadCurrent();
      await refreshIntegrityBadge();
      return;
    }
    if (result.status === 202) {
      setUploadStatus("结果不确定：资源可能已就位，元数据未确认。", "error");
      toast("上传结果不确定。请运行完整性核对以确认状态。", "error");
      await refreshIntegrityBadge();
      return;
    }
    if (result.status >= 400 && result.status < 500) {
      // A definite rejection will not become valid by replaying the same id.
      pendingUploadOperations.delete(pending.key);
    }
    setUploadStatus(`上传失败：${errorText(result)}`, "error");
    toast(`上传失败：${errorText(result)}`, "error");
  }

  async function toggleAttachment(attachment) {
    const target = attachment.visibility === "public" ? "private" : "public";
    const result = await api(`/api/attachments/${attachment.id}`, {
      method: "PATCH",
      body: { visibility: target },
    });
    if (!result.ok) {
      toast(`附件可见性未修改：${errorText(result)}`, "error");
      return;
    }
    await reloadCurrent();
    toast(target === "public" ? "附件已设为公开（仍需所属内容公开才可访问）。" : "附件已设为私有。");
  }

  // ------------------------------------------------------------ info

  function renderInfo() {
    const node = $("info-body");
    clear(node);
    const current = state.current;
    if (!current) {
      node.append(el("div", "info-key", "状态"), el("div", "info-value", "尚未保存的新记录"));
      return;
    }
    const rows = [
      ["内容身份", current.id],
      ["状态", current.state === "deleted" ? "回收站" : "有效"],
      ["类型", `${current.type} · ${CONTENT_TYPE_LABELS[current.type] || ""}`],
      ["主要空间", `${current.primary_space} · ${SPACE_LABELS[current.primary_space] || ""}`],
      ["关联空间", (current.related_spaces || []).join(" / ") || "—"],
      ["生命周期", `${current.lifecycle} · ${LIFECYCLE_LABELS[current.lifecycle] || ""}`],
      ["可见性", `${current.visibility}${current.visibility_selected ? "" : "（未选择，按私有）"}`],
      ["公开访问", current.public ? "是" : "否"],
      ["版本", String(current.revision)],
      ["创建时间", formatTime(current.created_at)],
      ["更新时间", formatTime(current.updated_at)],
      ["删除时间", current.deleted_at ? formatTime(current.deleted_at) : "—"],
      ["关系数", String(current.relations.length)],
      ["属性数", String(current.attributes.length)],
      ["附件数", String(current.attachments.length)],
      ["数据目录", state.dataDir || "—"],
    ];
    for (const [key, value] of rows) {
      node.append(el("div", "info-key", key), el("div", "info-value", value));
    }
  }

  // ------------------------------------------------------------ integrity

  async function refreshIntegrityBadge() {
    const result = await api("/api/integrity");
    if (!result.ok) return;
    state.integrity = result.data;
    const count = Number(result.data.active_open_issue_count || 0);
    const badge = $("integrity-badge");
    badge.textContent = String(count);
    badge.classList.toggle("hidden", count === 0);
  }

  function renderIntegrity() {
    const node = $("integrity-content");
    clear(node);
    const data = state.integrity;
    if (!data) {
      node.append(el("p", "drawer-empty", "尚未读取完整性状态。"));
      return;
    }

    const nonReady = Array.isArray(data.non_ready_attachments)
      ? data.non_ready_attachments
      : [];
    const lastReport = data.last_report;
    // Quarantine ledger rows stay visible as history, but only active-area
    // findings count as unresolved problems.
    const activeIssues = Array.isArray(data.active_open_issues)
      ? data.active_open_issues
      : data.open_issues;
    const summary = el("div", "integrity-summary");
    const stats = [
      [String(activeIssues.length), "未解决问题"],
      [String(data.quarantine_history_count || 0), "隔离区记录"],
      [String(nonReady.length), "非 READY 附件"],
      [
        lastReport && lastReport.counts ? String(lastReport.counts.issues) : "—",
        "上次扫描问题",
      ],
      [
        lastReport ? String(lastReport.quarantine_inventory_count) : "—",
        "上次核对隔离文件",
      ],
      [
        lastReport && lastReport.counts
          ? String(lastReport.counts.pending_operations)
          : "—",
        "上次核对未完成操作",
      ],
    ];
    for (const [value, label] of stats) {
      const stat = el("div", "stat");
      stat.append(el("div", "stat-value", value), el("div", "stat-label", label));
      summary.append(stat);
    }
    node.append(summary);

    const attachmentSection = el("div", "integrity-section");
    attachmentSection.append(el("h3", "", "非 READY 附件"));
    if (nonReady.length === 0) {
      attachmentSection.append(el("p", "drawer-empty", "当前没有非 READY 附件。"));
    } else {
      for (const attachment of nonReady) {
        const box = el("div", "integrity-record");
        box.append(
          el("div", "title", attachment.filename || "未命名附件"),
          el(
            "div",
            "meta",
            `${attachment.id} · 内容 ${attachment.content_id || "—"} · 操作 ${attachment.operation_id || "—"}`
          )
        );
        const stateText = STATE_LABELS[attachment.state] || attachment.state;
        box.append(
          el(
            "div",
            "detail",
            attachment.note ? `${stateText} · ${attachment.note}` : stateText
          )
        );
        box.append(el("div", "meta", `更新 ${formatTime(attachment.updated_at)}`));
        attachmentSection.append(box);
      }
    }
    node.append(attachmentSection);

    const issueSection = el("div", "integrity-section");
    issueSection.append(el("h3", "", "未解决问题"));
    if (activeIssues.length === 0) {
      issueSection.append(el("p", "drawer-empty", "当前没有登记在案的问题。"));
    } else {
      for (const issue of activeIssues) {
        const box = el("div", "integrity-issue");
        box.append(
          el("div", "kind", issue.kind),
          el("div", "target", issue.target),
          el("div", "detail", issue.detail)
        );
        issueSection.append(box);
      }
    }
    node.append(issueSection);

    const reportSection = el("div", "integrity-section");
    reportSection.append(el("h3", "", "上次核对（历史报告）"));
    if (!lastReport) {
      reportSection.append(
        el(
          "p",
          "drawer-empty",
          "还没有运行过核对。可在下方运行核对；恢复后的目录应重新运行核对。"
        )
      );
    } else {
      const report = lastReport;
      const line = el("p", "drawer-empty");
      line.textContent = `${formatTime(report.checked_at)} · 扫描 ${(report.scope || []).join(" / ")}`;
      reportSection.append(line);
      reportSection.append(
        el(
          "p",
          "integrity-note",
          "这是扫描时的历史报告，不代表恢复后的当前状态；恢复后的目录请重新运行核对。"
        )
      );
      const actions = Array.isArray(report.actions) ? report.actions : [];
      const issues = Array.isArray(report.issues) ? report.issues : [];
      if (actions.length === 0 && issues.length === 0) {
        reportSection.append(el("p", "drawer-empty", "扫描范围内未发现异常。"));
      }
      for (const action of actions) {
        reportSection.append(
          el("div", "record-sub", `恢复动作：${action.action} · ${action.attachment_id}`)
        );
      }
      for (const issue of issues) {
        reportSection.append(
          el("div", "record-sub", `${issue.kind} · ${issue.target} — ${issue.detail}`)
        );
      }
      const quarantined = Array.isArray(report.quarantined) ? report.quarantined : [];
      if (quarantined.length) {
        reportSection.append(
          el("div", "record-sub", `本次隔离 ${quarantined.length} 个文件（未删除）。`)
        );
      }
      const inventory = Array.isArray(report.quarantine_inventory)
        ? report.quarantine_inventory
        : [];
      if (inventory.length) {
        reportSection.append(el("h3", "", "隔离清单（只读）"));
        for (const entry of inventory) {
          const box = el("div", "integrity-record");
          box.append(el("div", "title", entry.name || "未命名隔离文件"));
          const source = [entry.origin || "unknown", entry.reason || "untracked"];
          if (entry.original_name) source.push(`原名称 ${entry.original_name}`);
          box.append(el("div", "meta", source.join(" · ")));
          box.append(
            el(
              "div",
              "meta",
              `${formatBytes(entry.bytes)} · SHA-256 ${entry.sha256 || "—"}`
            )
          );
          const tracking = entry.tracked_before_scan
            ? "既有记录"
            : entry.registered_this_run
              ? "本轮登记"
              : "未登记";
          box.append(
            el("div", "meta", `隔离时间 ${formatTime(entry.moved_at)} · ${tracking}`)
          );
          reportSection.append(box);
        }
      }
      if (report.note) reportSection.append(el("p", "integrity-note", report.note));
    }
    node.append(reportSection);

    if (data.recently_resolved.length) {
      const resolved = el("div", "integrity-section");
      resolved.append(el("h3", "", "最近已解决"));
      for (const issue of data.recently_resolved) {
        resolved.append(el("div", "record-sub", `${issue.kind} · ${issue.target}`));
      }
      node.append(resolved);
    }
  }

  async function openIntegrity() {
    $("drawer-backdrop").classList.remove("hidden");
    $("integrity-drawer").classList.remove("hidden");
    const result = await api("/api/integrity");
    if (result.ok) {
      state.integrity = result.data;
      renderIntegrity();
      refreshIntegrityBadge();
    } else {
      $("integrity-content").textContent = `完整性状态读取失败：${errorText(result)}`;
    }
  }

  function closeIntegrity() {
    $("drawer-backdrop").classList.add("hidden");
    $("integrity-drawer").classList.add("hidden");
  }

  async function reconcile() {
    const button = $("reconcile-button");
    button.disabled = true;
    $("reconcile-status").textContent = "正在扫描…";
    $("reconcile-status").className = "save-status";
    const result = await api("/api/integrity/reconcile", { method: "POST" });
    button.disabled = false;
    if (!result.ok) {
      $("reconcile-status").textContent = `核对失败：${errorText(result)}`;
      $("reconcile-status").className = "save-status error";
      return;
    }
    const report = result.data;
    $("reconcile-status").textContent = report.ok
      ? `扫描完成：未发现问题，隔离 ${report.counts.quarantined_this_run} 个文件。`
      : `扫描完成：${report.counts.issues} 个问题，隔离 ${report.counts.quarantined_this_run} 个文件。`;
    $("reconcile-status").className = `save-status ${report.ok ? "ok" : "error"}`;
    await refreshIntegrityBadge();
    const fresh = await api("/api/integrity");
    if (fresh.ok) {
      state.integrity = fresh.data;
      renderIntegrity();
    }
    if (state.current && !state.isNew) await reloadCurrent();
  }

  async function exportArchive() {
    const button = $("export-button");
    button.disabled = true;
    button.textContent = "导出中…";
    const result = await api("/api/export", { method: "POST", body: {} });
    button.disabled = false;
    button.textContent = "导出归档";
    if (!result.ok) {
      toast(`导出失败：${errorText(result)}`, "error");
      return;
    }
    const data = result.data;
    if (data.complete) {
      toast(`导出完成：${data.filename}（${formatBytes(data.bytes)}）`, "ok");
    } else {
      toast(
        `导出包已生成，但 ${data.counts.issues_error} 项无法完整导出，请核对 ${data.filename}`,
        "error"
      );
    }
    const link = document.createElement("a");
    link.href = data.download_path;
    link.download = data.filename;
    document.body.append(link);
    link.click();
    link.remove();
  }

  // ------------------------------------------------------------ tabs

  function activateTab(name) {
    for (const tab of document.querySelectorAll(".tab")) {
      const active = tab.dataset.tab === name;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    }
    for (const panel of document.querySelectorAll(".tab-panel")) {
      panel.classList.toggle("hidden", panel.id !== `panel-${name}`);
    }
  }

  // ------------------------------------------------------------ bindings

  function bind() {
    $("new-button").addEventListener("click", newDraft);
    $("empty-new-button").addEventListener("click", newDraft);
    $("overview-new").addEventListener("click", newDraft);
    $("edit-button").addEventListener("click", () => {
      if (!state.current || isReadonly()) return;
      state.mode = "edit";
      renderEditor(true);
      $("title-input").focus();
    });
    $("close-button").addEventListener("click", closeContent);
    $("save-button").addEventListener("click", saveCurrent);
    $("delete-button").addEventListener("click", deleteCurrent);
    $("restore-button").addEventListener("click", restoreCurrent);
    $("add-relation-button").addEventListener("click", addRelation);
    $("add-attribute-button").addEventListener("click", addAttribute);
    $("integrity-button").addEventListener("click", openIntegrity);
    $("export-button").addEventListener("click", exportArchive);
    $("integrity-close").addEventListener("click", closeIntegrity);
    $("drawer-backdrop").addEventListener("click", closeIntegrity);
    $("reconcile-button").addEventListener("click", reconcile);

    $("title-input").addEventListener("input", (event) => {
      state.draft.title = event.target.value;
      markDirty();
    });
    $("summary-input").addEventListener("input", (event) => {
      state.draft.summary = event.target.value;
      markDirty();
    });
    $("type-input").addEventListener("change", (event) => {
      state.draft.type = event.target.value;
      markDirty();
    });
    $("primary-space-input").addEventListener("change", (event) => {
      state.draft.primary_space = event.target.value;
      state.draft.related_spaces = state.draft.related_spaces.filter(
        (space) => space !== event.target.value
      );
      renderRelatedSpaces(isReadonly());
      markDirty();
    });
    $("lifecycle-input").addEventListener("change", (event) => {
      state.draft.lifecycle = event.target.value;
      markDirty();
    });
    $("body-input").addEventListener("input", (event) => {
      state.draft.body = event.target.value;
      markDirty();
    });

    for (const button of document.querySelectorAll(".space-button")) {
      button.addEventListener("click", () => chooseSpace(button.dataset.space));
    }

    $("content-list").addEventListener("keydown", (event) => {
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      const items = [...document.querySelectorAll(".content-item")];
      const index = items.indexOf(document.activeElement);
      if (index === -1) return;
      event.preventDefault();
      const next = items[index + (event.key === "ArrowDown" ? 1 : -1)];
      if (next) next.focus();
    });

    $("type-filter").addEventListener("change", async (event) => {
      state.typeFilter = event.target.value;
      await loadList();
    });
    $("lifecycle-filter").addEventListener("change", async (event) => {
      state.lifecycleFilter = event.target.value;
      await loadList();
    });

    for (const button of document.querySelectorAll(".visibility-button")) {
      button.addEventListener("click", () => {
        state.draft.visibility = button.dataset.visibility;
        for (const peer of document.querySelectorAll(".visibility-button")) {
          peer.classList.toggle("active", peer === button);
        }
        markDirty();
      });
    }

    for (const tab of document.querySelectorAll(".tab")) {
      tab.addEventListener("click", () => activateTab(tab.dataset.tab));
    }

    $("attachment-input").addEventListener("change", (event) => {
      const file = event.target.files && event.target.files[0];
      if (file) uploadAttachment(file);
    });

    let searchTimer = null;
    $("search-input").addEventListener("input", (event) => {
      const value = event.target.value;
      if (searchTimer) window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(async () => {
        state.query = value.trim();
        await loadList();
      }, 180);
    });

    $("trash-toggle").addEventListener("change", async (event) => {
      state.includeDeleted = event.target.checked;
      await loadList();
    });

    $("conflict-reload").addEventListener("click", async () => {
      const id = state.current ? state.current.id : null;
      if (!id) return;
      state.dirty = false;
      state.statusText = "";
      $("conflict-banner").classList.add("hidden");
      await selectContent(id);
      toast("已重新载入服务器版本，本地未保存的修改已丢弃。");
    });

    $("conflict-keep").addEventListener("click", () => {
      $("conflict-banner").classList.add("hidden");
      setStatus("内容已在别处更新；保存前需重新载入", "error");
    });

    $("confirm-ok").addEventListener("click", () => settleConfirm(true));
    $("confirm-cancel").addEventListener("click", () => settleConfirm(false));
    $("confirm-dialog").addEventListener("cancel", (event) => {
      event.preventDefault();
      settleConfirm(false);
    });

    window.addEventListener("beforeunload", (event) => {
      if (state.dirty) {
        event.preventDefault();
        event.returnValue = "";
      }
    });

    document.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        saveCurrent();
      }
    });
  }

  // ------------------------------------------------------------ init

  async function init() {
    bind();
    const session = await api("/api/session");
    if (!session.ok) {
      if (session.status !== 401) {
        toast(`初始化失败：${errorText(session)}`, "error");
      }
      return;
    }
    state.dataDir = session.data.data_dir;
    const badge = $("integrity-badge");
    const count = session.data.integrity.open_issue_count;
    badge.textContent = String(count);
    badge.classList.toggle("hidden", count === 0);
    await refreshOverview();
    await loadList();
    await loadDrift();
  }

  init();
})();
