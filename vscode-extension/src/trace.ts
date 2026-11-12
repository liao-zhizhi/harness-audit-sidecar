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
}

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

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
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
