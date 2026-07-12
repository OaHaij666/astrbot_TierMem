const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const make = (tag, text, className) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
};

const demoGraph = {
  nodes: [
    { id: "user:1001", name: "林舟", type: "user", aliases: ["小林"] },
    { id: "user:1002", name: "小王", type: "user", aliases: ["王明"] },
    {
      id: "project:tiermem",
      name: "TierMem",
      type: "project",
      aliases: ["记忆插件"],
    },
    { id: "group:789", name: "开发群", type: "group", aliases: [] },
  ],
  edges: [
    {
      id: "r1",
      source: "user:1001",
      target: "user:1002",
      type: "colleague_of",
      strength: 0.86,
      confidence: 0.9,
      scope: "group",
    },
    {
      id: "r2",
      source: "user:1002",
      target: "project:tiermem",
      type: "participates_in",
      strength: 0.78,
      confidence: 0.88,
      scope: "public",
    },
    {
      id: "r3",
      source: "user:1001",
      target: "group:789",
      type: "member_of",
      strength: 0.95,
      confidence: 0.98,
      scope: "group",
    },
  ],
};
const demoSettings = {
  fifo_size: 10,
  fifo_max_wait_minutes: 30,
  max_memories_per_user: 200,
  max_injected_memories: 24,
  max_injected_relations: 12,
  atom_fts_candidate_limit: 40,
  atom_like_candidate_limit: 24,
  atom_background_limit: 4,
  atom_query_term_limit: 24,
  max_concurrent_summaries: 2,
  summary_provider_id: "",
  summary_system_prompt: "",
  graph_recall_max_hops: 2,
  graph_alias_min_length: 2,
  graph_max_matched_entities: 6,
  graph_entity_scan_limit: 5000,
  retrieval_min_strength: 0.08,
  core_half_life_days: 0,
  semantic_half_life_days: 180,
  episodic_half_life_days: 45,
  working_half_life_days: 7,
  relation_half_life_days: 180,
  enable_auto_summary: true,
  enable_manual_summary: true,
  enable_llm_tools: true,
  tool_caution_in_prompt: true,
  inject_memory_in_private: true,
  inject_memory_in_group: true,
  inject_fifo_in_group: true,
  relation_intent_keywords: {
    friend_of: ["朋友", "好友"],
    colleague_of: ["同事", "共事"],
    participates_in: ["参与", "项目", "负责"],
  },
};
const demoMemories = [
  {
    user_id: "1001",
    layer: "core",
    content: "用户是后端开发者",
    importance: 5,
    strength: 0.96,
    confidence: 0.95,
    updated_at: "2026-07-12T10:20:00+08:00",
  },
  {
    user_id: "1001",
    layer: "semantic",
    content: "用户喜欢简洁、可解释的技术方案",
    importance: 4,
    strength: 0.78,
    confidence: 0.88,
    updated_at: "2026-07-12T10:10:00+08:00",
  },
  {
    user_id: "1002",
    layer: "working",
    content: "正在整理 TierMem 的图谱召回逻辑",
    importance: 3,
    strength: 0.62,
    confidence: 0.8,
    updated_at: "2026-07-12T09:55:00+08:00",
  },
];

const mockBridge = {
  async ready() {
    return { isDark: true, locale: "zh-CN" };
  },
  onContext() {
    return () => {};
  },
  t(_key, fallback) {
    return fallback;
  },
  async apiGet(endpoint) {
    if (endpoint === "stats")
      return {
        memories: 128,
        users: 18,
        entities: 34,
        relations: 42,
        fifo: 7,
        layers: { core: 20, semantic: 61, episodic: 31, working: 16 },
        fts: { available: true, tokenizer: "trigram" },
      };
    if (endpoint === "graph") return demoGraph;
    if (endpoint === "memories") return { items: demoMemories };
    if (endpoint === "settings") return demoSettings;
    return {};
  },
  async apiPost(endpoint, body) {
    if (endpoint === "settings") return { saved: true };
    if (endpoint === "recall")
      return {
        query: body.message,
        search: {
          mode: "fts5",
          query_terms: ["小王负", "王负责", "tiermem"],
          fts_available: true,
          tokenizer: "trigram",
        },
        atoms: [
          {
            memory_id: "mem_demo_1",
            content: "小王正在负责 TierMem 项目的召回模块",
            layer: "episodic",
            category: "relation",
            score: 0.842,
            components: {
              text: 1,
              strength: 0.78,
              importance: 0.8,
              confidence: 0.88,
            },
          },
        ],
        atom_entities: [
          {
            memory_id: "mem_demo_1",
            entities: [
              { id: "user:1002", name: "小王" },
              { id: "project:tiermem", name: "TierMem" },
            ],
          },
        ],
        evidence_edges: [
          {
            memory_id: "mem_demo_1",
            relation_id: "r2",
            source: "user:1002",
            target: "project:tiermem",
            type: "participates_in",
            polarity: "support",
            weight: 0.88,
          },
        ],
        matched_entities: [
          {
            id: "user:1002",
            name: "小王",
            alias: "小王",
            kind: "name",
            score: 105,
          },
          {
            id: "project:tiermem",
            name: "TierMem",
            alias: "tiermem",
            kind: "name",
            score: 80,
          },
        ],
        intents: ["participates_in"],
        relations: [
          {
            id: "r2",
            source: "user:1002",
            target: "project:tiermem",
            type: "participates_in",
            score: 137.2,
            reasons: ["连接消息实体", "直接连接两个锚点", "关系意图匹配"],
            strength: 0.78,
            confidence: 0.88,
            hop: 1,
            supporting_memory_ids: ["mem_demo_1"],
          },
        ],
        request: body,
      };
    return {};
  },
};

const bridge = window.AstrBotPluginPage || mockBridge;
await bridge.ready();
const state = {
  graph: { nodes: [], edges: [] },
  settings: {},
  activeView: "overview",
  selectedNode: null,
};

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    node.hidden = true;
  }, 3600);
}

async function withBusy(button, task) {
  const old = button.textContent;
  button.disabled = true;
  button.textContent = "处理中…";
  try {
    return await task();
  } finally {
    button.disabled = false;
    button.textContent = old;
  }
}

function switchView(name) {
  state.activeView = name;
  $$(".tab").forEach((tab) => {
    const active = tab.dataset.view === name;
    tab.classList.toggle("active", active);
    if (active) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  });
  $$(".view").forEach((view) => {
    const active = view.id === `view-${name}`;
    view.hidden = !active;
    view.classList.toggle("active", active);
  });
  if (name === "graph") loadGraph();
  if (name === "memories") loadMemories();
  if (name === "settings") loadSettings();
}

$$(".tab").forEach((tab) =>
  tab.addEventListener("click", () => switchView(tab.dataset.view)),
);

async function loadOverview() {
  const stats = await bridge.apiGet("stats");
  const labels = {
    memories: "活跃记忆",
    users: "记忆用户",
    entities: "图谱实体",
    relations: "活跃关系",
    fifo: "待总结轮次",
  };
  const metrics = $("#metrics");
  const cards = Object.entries(labels).map(([key, label]) => {
      const card = make("article", undefined, "metric");
      card.append(make("span", label), make("strong", String(stats[key] ?? 0)));
      return card;
    });
  const fts = stats.fts || { available: false, tokenizer: "like" };
  const ftsCard = make("article", undefined, "metric status-metric");
  ftsCard.append(
    make("span", "原子文本索引"),
    make("strong", fts.available ? `FTS5 · ${fts.tokenizer}` : "LIKE 降级"),
  );
  cards.push(ftsCard);
  metrics.replaceChildren(...cards);
  const layers = stats.layers || {};
  const max = Math.max(1, ...Object.values(layers));
  const container = $("#layer-bars");
  container.replaceChildren(
    ...["core", "semantic", "episodic", "working"].map((layer) => {
      const row = make("div", undefined, "layer-row");
      const track = make("div", undefined, "layer-track");
      const fill = make("div", undefined, "layer-fill");
      fill.style.width = `${((layers[layer] || 0) / max) * 100}%`;
      track.append(fill);
      const output = make("output", String(layers[layer] || 0));
      row.append(make("span", layer), track, output);
      return row;
    }),
  );
}

const graphCanvas = $("#graph-canvas");
const graphCtx = graphCanvas.getContext("2d");
let graphNodes = [];
let graphEdges = [];
let dragged = null;

function graphColors() {
  const css = getComputedStyle(document.documentElement);
  return {
    user: css.getPropertyValue("--node-user"),
    group: css.getPropertyValue("--node-group"),
    project: css.getPropertyValue("--node-project"),
    organization: css.getPropertyValue("--node-project"),
    topic: css.getPropertyValue("--node-other"),
    other: css.getPropertyValue("--node-other"),
    text: css.getPropertyValue("--text"),
    border: css.getPropertyValue("--border"),
    surface: css.getPropertyValue("--surface"),
  };
}

function layoutGraph(nodes, width, height) {
  const groups = {};
  nodes.forEach((node) => (groups[node.type] ||= []).push(node));
  const types = Object.keys(groups);
  types.forEach((type, groupIndex) => {
    groups[type].forEach((node, index) => {
      if (Number.isFinite(node.x) && Number.isFinite(node.y)) return;
      const angle =
        (Math.PI * 2 * index) / Math.max(1, groups[type].length) +
        groupIndex * 0.75;
      const radius = Math.min(width, height) * (0.18 + (groupIndex % 3) * 0.1);
      node.x = width / 2 + Math.cos(angle) * radius;
      node.y = height / 2 + Math.sin(angle) * radius;
    });
  });
}

function drawGraph() {
  const rect = graphCanvas.getBoundingClientRect();
  const scale = window.devicePixelRatio || 1;
  graphCanvas.width = Math.max(1, Math.floor(rect.width * scale));
  graphCanvas.height = Math.max(1, Math.floor(rect.height * scale));
  graphCtx.setTransform(scale, 0, 0, scale, 0, 0);
  graphCtx.clearRect(0, 0, rect.width, rect.height);
  layoutGraph(graphNodes, rect.width, rect.height);
  const colors = graphColors();
  const byId = Object.fromEntries(graphNodes.map((node) => [node.id, node]));
  graphEdges.forEach((edge) => {
    const source = byId[edge.source],
      target = byId[edge.target];
    if (!source || !target) return;
    graphCtx.beginPath();
    graphCtx.moveTo(source.x, source.y);
    graphCtx.lineTo(target.x, target.y);
    graphCtx.strokeStyle = colors.border;
    graphCtx.globalAlpha = 0.7;
    graphCtx.lineWidth = 1.2;
    graphCtx.stroke();
    graphCtx.globalAlpha = 1;
    const mx = (source.x + target.x) / 2,
      my = (source.y + target.y) / 2;
    graphCtx.fillStyle = colors.surface;
    graphCtx.fillRect(mx - 36, my - 9, 72, 18);
    graphCtx.fillStyle = colors.text;
    graphCtx.font = "11px ui-monospace";
    graphCtx.textAlign = "center";
    graphCtx.fillText(edge.type.slice(0, 16), mx, my + 4);
  });
  graphNodes.forEach((node) => {
    const selected = state.selectedNode === node.id;
    graphCtx.beginPath();
    graphCtx.arc(node.x, node.y, selected ? 12 : 9, 0, Math.PI * 2);
    graphCtx.fillStyle = colors[node.type] || colors.other;
    graphCtx.fill();
    if (selected) {
      graphCtx.strokeStyle = colors.text;
      graphCtx.lineWidth = 3;
      graphCtx.stroke();
    }
    graphCtx.fillStyle = colors.text;
    graphCtx.font = "12px system-ui";
    graphCtx.textAlign = "center";
    graphCtx.fillText(node.name || node.id, node.x, node.y + 26);
  });
}

function filterGraph() {
  const query = $("#graph-search").value.trim().toLocaleLowerCase();
  const type = $("#graph-type").value;
  const allNodes = state.graph.nodes || [];
  const allEdges = state.graph.edges || [];
  const matching = new Set(
    allNodes
      .filter(
        (node) =>
          (!type || node.type === type) &&
          (!query ||
            `${node.name} ${node.id} ${(node.aliases || []).join(" ")}`
              .toLocaleLowerCase()
              .includes(query)),
      )
      .map((node) => node.id),
  );
  graphEdges = allEdges.filter((edge) => {
    const relationMatch =
      !query || edge.type.toLocaleLowerCase().includes(query);
    return (
      (matching.has(edge.source) && matching.has(edge.target)) ||
      (relationMatch &&
        (!type || matching.has(edge.source) || matching.has(edge.target)))
    );
  });
  const used = new Set(
    graphEdges.flatMap((edge) => [edge.source, edge.target]),
  );
  graphNodes = allNodes.filter(
    (node) => used.has(node.id) || (matching.has(node.id) && !allEdges.length),
  );
  $("#graph-empty").hidden = graphNodes.length > 0;
  renderEdgeTable();
  drawGraph();
}

function renderEdgeTable() {
  const names = Object.fromEntries(
    (state.graph.nodes || []).map((node) => [node.id, node.name || node.id]),
  );
  const tbody = $("#edge-table");
  tbody.replaceChildren(
    ...graphEdges.map((edge) => {
      const row = document.createElement("tr");
      [
        names[edge.source] || edge.source,
        edge.type,
        names[edge.target] || edge.target,
        Number(edge.strength).toFixed(2),
        Number(edge.confidence).toFixed(2),
        edge.scope,
      ].forEach((value) => row.append(make("td", value)));
      return row;
    }),
  );
  $("#edge-count").textContent = `${graphEdges.length} 条关系`;
}

async function loadGraph() {
  state.graph = await bridge.apiGet("graph", { limit: 240 });
  filterGraph();
}

function canvasPoint(event) {
  const rect = graphCanvas.getBoundingClientRect();
  return { x: event.clientX - rect.left, y: event.clientY - rect.top };
}
function nearestNode(point) {
  return graphNodes.find(
    (node) => Math.hypot(node.x - point.x, node.y - point.y) <= 18,
  );
}
graphCanvas.addEventListener("pointerdown", (event) => {
  dragged = nearestNode(canvasPoint(event));
  if (dragged) graphCanvas.setPointerCapture(event.pointerId);
});
graphCanvas.addEventListener("pointermove", (event) => {
  if (!dragged) return;
  const p = canvasPoint(event);
  dragged.x = p.x;
  dragged.y = p.y;
  drawGraph();
});
graphCanvas.addEventListener("pointerup", (event) => {
  const node = dragged || nearestNode(canvasPoint(event));
  dragged = null;
  if (!node) return;
  state.selectedNode = node.id;
  const count = graphEdges.filter(
    (edge) => edge.source === node.id || edge.target === node.id,
  ).length;
  $("#node-detail").textContent =
    `${node.name} · ${node.id} · ${node.type} · ${count} 条可见关系`;
  drawGraph();
});
new ResizeObserver(drawGraph).observe(graphCanvas);
$("#graph-search").addEventListener("input", filterGraph);
$("#graph-type").addEventListener("change", filterGraph);

async function loadMemories() {
  const result = await bridge.apiGet("memories", {
    limit: 300,
    user_id: $("#memory-user").value.trim(),
    layer: $("#memory-layer").value,
  });
  const items = result.items || [];
  $("#memory-table").replaceChildren(
    ...items.map((item) => {
      const row = document.createElement("tr");
      [
        item.user_id,
        item.layer,
        item.content,
        item.importance,
        Number(item.strength).toFixed(2),
        Number(item.confidence).toFixed(2),
        new Date(item.updated_at).toLocaleString(),
      ].forEach((value) => row.append(make("td", String(value))));
      return row;
    }),
  );
  $("#memory-empty").hidden = items.length > 0;
}
$("#memory-filter").addEventListener("click", loadMemories);

function recallSection(title) {
  const section = make("section", undefined, "recall-group");
  section.append(make("h3", title));
  return section;
}

function scoreText(value) {
  return Number(value || 0).toFixed(3);
}

$("#recall-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  await withBusy(button, async () => {
    const data = Object.fromEntries(new FormData(event.currentTarget));
    const result = await bridge.apiPost("recall", data);
    const output = $("#recall-output");
    output.replaceChildren();

    const searchGroup = recallSection("01 · 原子文本检索");
    const searchMeta = make("div", undefined, "recall-meta");
    const search = result.search || {};
    searchMeta.append(
      make("span", `模式 ${search.mode || "unknown"}`, "chip"),
      make(
        "span",
        search.fts_available
          ? `FTS5 ${search.tokenizer || ""}`
          : "FTS 不可用 · LIKE 降级",
        "chip secondary-chip",
      ),
    );
    searchGroup.append(searchMeta);
    const terms = (search.query_terms || []).join(" · ");
    searchGroup.append(
      make("p", terms ? `检索词：${terms}` : "没有生成可用检索词", "muted"),
    );

    const atomGroup = recallSection("02 · 命中的记忆原子");
    (result.atoms || []).forEach((atom) => {
      const card = make("article", undefined, "recall-edge atom-card");
      card.append(
        make(
          "strong",
          `${atom.memory_id} · ${atom.layer}/${atom.category} · ${scoreText(atom.score)}`,
        ),
        make("p", atom.content),
      );
      const scores = make("div", undefined, "score-components");
      Object.entries(atom.components || {}).forEach(([key, value]) =>
        scores.append(make("span", `${key} ${scoreText(value)}`)),
      );
      card.append(scores);
      atomGroup.append(card);
    });
    if (!(result.atoms || []).length)
      atomGroup.append(make("p", "没有可注入的活跃原子", "muted"));

    const entryGroup = recallSection("03 · 原子进入知识图");
    (result.atom_entities || []).forEach((mapping) => {
      const entities = (mapping.entities || [])
        .map((entity) => `${entity.name} (${entity.id})`)
        .join(" · ");
      entryGroup.append(
        make(
          "div",
          `${mapping.memory_id} → ${entities || "未映射实体"}`,
          "trace-row",
        ),
      );
    });
    (result.evidence_edges || []).forEach((edge) =>
      entryGroup.append(
        make(
          "div",
          `${edge.memory_id} → ${edge.source} --${edge.type}--> ${edge.target} · ${edge.polarity}`,
          "trace-row evidence-row",
        ),
      ),
    );
    if (
      !(result.atom_entities || []).length &&
      !(result.evidence_edges || []).length
    )
      entryGroup.append(make("p", "原子没有关联图实体或证据边", "muted"));

    const entityGroup = recallSection("04 · 并行精确实体与意图");
    const chips = make("div", undefined, "chips");
    (result.matched_entities || []).forEach((entity) =>
      chips.append(
        make(
          "span",
          `${entity.name} · ${entity.kind} · ${entity.score}`,
          "chip",
        ),
      ),
    );
    if (!chips.childNodes.length)
      chips.append(make("span", "没有命中实体", "muted"));
    entityGroup.append(chips);
    const intentChips = make("div", undefined, "chips");
    (result.intents || []).forEach((intent) =>
      intentChips.append(make("span", intent, "chip")),
    );
    if (!intentChips.childNodes.length)
      intentChips.append(make("span", "未识别关系意图", "muted"));
    entityGroup.append(intentChips);

    const edgeGroup = recallSection("05 · 最终图关系");
    (result.relations || []).forEach((edge) => {
      const card = make("div", undefined, "recall-edge");
      card.append(
        make(
          "strong",
          `${edge.source} --${edge.type}--> ${edge.target} · ${scoreText(edge.score)} · ${edge.hop || 1} hop`,
        ),
        make("p", (edge.reasons || []).join(" / ")),
      );
      if ((edge.supporting_memory_ids || []).length)
        card.append(
          make(
            "p",
            `证据原子：${edge.supporting_memory_ids.join(" · ")}`,
            "supporting-atoms",
          ),
        );
      edgeGroup.append(card);
    });
    if (!(result.relations || []).length)
      edgeGroup.append(make("p", "没有召回关系", "muted"));
    output.append(searchGroup, atomGroup, entryGroup, entityGroup, edgeGroup);
  }).catch((error) => toast(`召回失败：${error.message}`));
});

const settingGroups = {
  "#settings-queue": [
    ["fifo_size", "FIFO 条数阈值", 1],
    ["fifo_max_wait_minutes", "FIFO 最大等待（分钟）", 0.1],
    ["max_memories_per_user", "单用户记忆上限", 1],
    ["max_injected_memories", "注入记忆数", 1],
  ],
  "#settings-recall": [
    ["max_injected_relations", "注入关系数", 1],
    ["atom_fts_candidate_limit", "FTS 初筛候选", 1],
    ["atom_like_candidate_limit", "LIKE 降级候选", 1],
    ["atom_background_limit", "背景兜底原子", 1],
    ["atom_query_term_limit", "最大检索词数", 1],
    ["graph_recall_max_hops", "最大图跳数", 1],
    ["graph_alias_min_length", "最短别名长度", 1],
    ["graph_max_matched_entities", "最大实体候选", 1],
    ["graph_entity_scan_limit", "实体扫描上限", 1],
    ["retrieval_min_strength", "最低有效强度", 0.01],
  ],
  "#settings-decay": [
    ["core_half_life_days", "Core 半衰期（天）", 0.1],
    ["semantic_half_life_days", "Semantic 半衰期（天）", 0.1],
    ["episodic_half_life_days", "Episodic 半衰期（天）", 0.1],
    ["working_half_life_days", "Working 半衰期（天）", 0.1],
    ["relation_half_life_days", "关系半衰期（天）", 0.1],
  ],
};
const switchSettings = [
  ["enable_auto_summary", "自动总结"],
  ["enable_manual_summary", "手动总结"],
  ["enable_llm_tools", "LLM 记忆工具"],
  ["tool_caution_in_prompt", "工具边界提示"],
  ["inject_memory_in_private", "私聊注入"],
  ["inject_memory_in_group", "群聊注入"],
  ["inject_fifo_in_group", "群聊 FIFO 注入"],
];

function buildSettingsForm() {
  Object.entries(settingGroups).forEach(([selector, fields]) => {
    const group = $(selector);
    if (group.childNodes.length) return;
    fields.forEach(([key, label, step]) => {
      const wrap = document.createElement("label");
      wrap.textContent = label;
      const input = document.createElement("input");
      input.type = "number";
      input.name = key;
      input.step = String(step);
      input.dataset.kind = "number";
      wrap.append(input);
      group.append(wrap);
    });
  });
  const switches = $("#settings-switches");
  if (!switches.childNodes.length)
    switchSettings.forEach(([key, label]) => {
      const wrap = make("label", undefined, "switch-control");
      wrap.append(make("span", label));
      const input = document.createElement("input");
      input.type = "checkbox";
      input.name = key;
      input.dataset.kind = "boolean";
      wrap.append(input);
      switches.append(wrap);
    });
}

async function loadSettings() {
  buildSettingsForm();
  state.settings = await bridge.apiGet("settings");
  $("#settings-form")
    .querySelectorAll("input[name], textarea[name]")
    .forEach((input) => {
      const value = state.settings[input.name];
      if (input.type === "checkbox") input.checked = Boolean(value);
      else input.value = value ?? "";
    });
  $("#intent-keywords").value = JSON.stringify(
    state.settings.relation_intent_keywords || {},
    null,
    2,
  );
}

$("#settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  await withBusy(button, async () => {
    const payload = {};
    event.currentTarget
      .querySelectorAll("input[name], textarea[name]")
      .forEach((input) => {
        payload[input.name] =
          input.type === "checkbox"
            ? input.checked
            : input.type === "number"
              ? Number(input.value)
              : input.value;
      });
    try {
      payload.relation_intent_keywords = JSON.parse(
        $("#intent-keywords").value,
      );
    } catch {
      throw new Error("关系意图词典不是有效 JSON");
    }
    await bridge.apiPost("settings", payload);
    $("#settings-state").textContent = "配置已保存并即时生效";
    toast("TierMem 配置已保存");
  }).catch((error) => {
    $("#settings-state").textContent = error.message;
    toast(`保存失败：${error.message}`);
  });
});

$("#refresh-button").addEventListener("click", (event) =>
  withBusy(event.currentTarget, async () => {
    await loadOverview();
    if (state.activeView === "graph") await loadGraph();
    if (state.activeView === "memories") await loadMemories();
    toast("数据已刷新");
  }),
);
bridge.onContext?.((context) => {
  document.documentElement.dataset.theme = context?.isDark ? "dark" : "light";
  drawGraph();
});
document.title =
  bridge.t?.("pages.tiermem-console.title", "TierMem 控制台") ||
  "TierMem 控制台";
$("#connection-label").textContent = window.AstrBotPluginPage
  ? "已连接 AstrBot"
  : "本地预览数据";
await loadOverview();
