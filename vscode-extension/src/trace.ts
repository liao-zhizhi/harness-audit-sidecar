/**
 * Same panel as ``harness-audit report --lang zh``, rendered as HTML.
 */

export interface TraceRow {
  timestamp?: string;
  tool?: string;
  intent?: string;
  verdict?: string;
  reason?: string;
  missing_expected?: unknown;
  unexpected_changes?: unknown;
  changed_files?: unknown;
  relative_path?: unknown;
  path?: unknown;
}

export interface L0Event {
  timestamp: string;
  path: string;
  button: string;
}

export interface PendingFail {
  timestamp: string;
  path: string;
  basename: string;
  timeLabel: string;
  verdict: "FAIL" | "INVALID";
}

export const L0_EVENTS_REL = "notes/l0_events.md";
export const PENDING_CAP = 5;

export function loadTrace(text: string): TraceRow[] {
  const rows: TraceRow[] = [];
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim()) {
      continue;
    }
    rows.push(JSON.parse(line) as TraceRow);
  }
  return rows;
}

export function verdictKey(row: TraceRow): string {
  const value = String(row.verdict || "");
  if (value === "NOT_RUN" || value === "NOT RUN") {
    return "NOT RUN";
  }
  return value;
}

export function countVerdicts(rows: TraceRow[]): Record<string, number> {
  const counts = { PASS: 0, FAIL: 0, INVALID: 0, "NOT RUN": 0 };
  for (const row of rows) {
    const key = verdictKey(row);
    if (key in counts) {
      counts[key as keyof typeof counts] += 1;
    }
  }
  return counts;
}

function files(values: unknown): string {
  if (!values) {
    return "（无）";
  }
  if (typeof values === "string") {
    return values || "（无）";
  }
  if (Array.isArray(values) && values.length) {
    return values.map(String).join(", ");
  }
  return "（无）";
}

export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function firstPath(value: unknown): string {
  if (typeof value === "string" && value.trim()) {
    return value.trim().replace(/^\.\//, "");
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = firstPath(item);
      if (found) {
        return found;
      }
    }
  }
  return "";
}

const INTENT_PATH = /(?:modify file|append file|append to)\s+(\S+)/i;
const RELATIVE_PATH =
  /(?:^|[\s"'=])((?:sentience_live|experiments|notes|scripts|docs|fake_dir|my_new_dir)\/[^\s"']+)/;
const ABSOLUTE_PATH = /(\/(?:[^\s"'\\;|&<>]+)+)/;

export function pathOf(row: TraceRow): string {
  const found =
    firstPath(row.relative_path) ||
    firstPath(row.path) ||
    firstPath(row.missing_expected) ||
    firstPath(row.changed_files);
  if (found) {
    return found;
  }
  const intent = String(row.intent || "");
  const fromIntent = INTENT_PATH.exec(intent);
  if (fromIntent) {
    return fromIntent[1].replace(/^\.\//, "");
  }
  const relative = RELATIVE_PATH.exec(intent);
  if (relative) {
    return relative[1].replace(/["']$/, "");
  }
  const absolute = ABSOLUTE_PATH.exec(intent);
  if (absolute) {
    return absolute[1];
  }
  const command = intent.replace(/^run command:\s*/i, "").trim();
  return command ? command.slice(0, 120) : "";
}

export function basenameOf(filePath: string): string {
  const trimmed = filePath.replace(/\\/g, "/").replace(/\/+$/, "");
  const parts = trimmed.split("/");
  return parts[parts.length - 1] || trimmed || "(no path)";
}

export function timeLabelOf(timestamp: string): string {
  const match = /(\d{2}:\d{2})/.exec(timestamp);
  return match ? match[1] : timestamp || "—";
}

export function parseL0Events(text: string): L0Event[] {
  const events: L0Event[] = [];
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim() || line.trim().startsWith("#")) {
      continue;
    }
    const parts = line.split("|").map((part) => part.trim());
    if (parts.length < 3) {
      continue;
    }
    events.push({ timestamp: parts[0], path: parts[1], button: parts.slice(2).join(" | ") });
  }
  return events;
}

export function eventKey(timestamp: string, filePath: string): string {
  return `${timestamp}|${filePath}`;
}

export function formatL0EventLine(timestamp: string, filePath: string, button: string): string {
  return `${timestamp} | ${filePath} | ${button}`;
}

export function pendingFails(rows: TraceRow[], events: L0Event[]): PendingFail[] {
  const done = new Set(events.map((item) => eventKey(item.timestamp, item.path)));
  const pending: PendingFail[] = [];
  for (let index = rows.length - 1; index >= 0; index -= 1) {
    const row = rows[index];
    const verdict = String(row.verdict || "");
    if (verdict !== "FAIL" && verdict !== "INVALID") {
      continue;
    }
    const filePath = pathOf(row);
    const timestamp = String(row.timestamp || "");
    if (!timestamp || !filePath) {
      continue;
    }
    if (done.has(eventKey(timestamp, filePath))) {
      continue;
    }
    pending.push({
      timestamp,
      path: filePath,
      basename: basenameOf(filePath),
      timeLabel: timeLabelOf(timestamp),
      verdict: verdict === "INVALID" ? "INVALID" : "FAIL",
    });
  }
  return pending;
}

export function renderPendingRow(item: PendingFail): string {
  const path = escapeHtml(item.path);
  const name = escapeHtml(item.basename);
  const time = escapeHtml(item.timeLabel);
  const ts = escapeHtml(item.timestamp);
  const kind = item.verdict === "INVALID" ? "invalid" : "fail";
  const mark = item.verdict === "INVALID" ? "INV" : "FAIL";
  return `<div class="pending-row" data-ts="${ts}" data-path="${path}">
    <span class="pending-dot pending-dot-${kind}" title="${mark}"></span>
    <span class="pending-mark pending-mark-${kind}">${mark}</span>
    <span class="pending-name" title="${path}">${name}</span>
    <span class="pending-time">${time}</span>
    <span class="pending-btns">
      <button type="button" data-btn="重做">重做</button>
      <button type="button" data-btn="跳过">跳过</button>
      <button type="button" data-btn="不确定">不确定</button>
    </span>
  </div>`;
}

export function renderProcessedRow(item: L0Event): string {
  return `<div class="processed-row">
    <span class="pending-name" title="${escapeHtml(item.path)}">${escapeHtml(basenameOf(item.path))}</span>
    <span class="pending-time">${escapeHtml(timeLabelOf(item.timestamp))}</span>
    <span class="processed-btn">${escapeHtml(item.button)}</span>
  </div>`;
}

const BADGE: Record<string, { label: string; cls: string }> = {
  PASS: { label: "【PASS】", cls: "pass" },
  FAIL: { label: "【FAIL】", cls: "fail" },
  INVALID: { label: "【INVALID】", cls: "invalid" },
  "NOT RUN": { label: "【NOT_RUN】", cls: "notrun" },
};

export function renderPanelHtml(rows: TraceRow[], limit = 20): string {
  if (!rows.length) {
    return `<p class="empty">当前目录没有可读的审计记录。接入后跑一次工具调用即可。</p>`;
  }
  const shown = limit > 0 ? rows.slice(-limit) : rows.slice();
  const counts = countVerdicts(shown);
  const cards = shown
    .map((row) => {
      const key = verdictKey(row);
      const badge = BADGE[key] || { label: `【${escapeHtml(key || "?")}】`, cls: "notrun" };
      const stamp = escapeHtml(String(row.timestamp || "—"));
      const tool = escapeHtml(String(row.tool || "—"));
      const intent = escapeHtml(String(row.intent || "—"));
      let extra = "";
      if (key === "FAIL") {
        extra = `
          <div class="fail-hl">原因: ${escapeHtml(String(row.reason || ""))}</div>
          <div class="fail-hl">没动的文件: ${escapeHtml(files(row.missing_expected))}</div>
          <div class="fail-hl">多动的文件: ${escapeHtml(files(row.unexpected_changes))}</div>`;
      }
      return `<article class="row">
        <div class="head">${stamp}  ${tool}  <span class="${badge.cls}">${badge.label}</span></div>
        <div>意图: ${intent}</div>${extra}
      </article>`;
    })
    .join("");
  return `
    <h2>harness-audit 审计面板</h2>
    ${cards}
    <p class="stats">最近 ${shown.length} 条：PASS ${counts.PASS}  FAIL ${counts.FAIL}  INVALID ${counts.INVALID}  NOT_RUN ${counts["NOT RUN"]}</p>
  `;
}
