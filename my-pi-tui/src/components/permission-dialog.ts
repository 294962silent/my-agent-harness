import {
  type Component,
  Container,
  matchesKey,
  SelectList,
  type SelectItem,
  type SelectListTheme,
  Spacer,
  Text,
  truncateToWidth,
} from "@earendil-works/pi-tui";
import type {
  PermissionRequestParams,
  PermissionResult,
  PermissionRisk,
} from "../client.js";
import { theme } from "../theme/theme.js";
import { DynamicBorder } from "./dynamic-border.js";
import { isEnterKey } from "./keys.js";

export const PERMISSION_OPTIONS: ReadonlyArray<SelectItem> = [
  {
    value: "once",
    label: "允许这一次",
    description: "仅批准本次工具调用",
  },
  {
    value: "session",
    label: "本会话都允许",
    description: "本会话内不再询问该工具",
  },
  {
    value: "deny",
    label: "拒绝",
    description: "阻止本次工具调用",
  },
];

const MAX_TARGET_CHARS = 96;
const MAX_PREVIEW_LINES = 12;

/** Collapse newlines/tabs and ellipsize long one-line previews (targets). */
export function truncateSingleLine(
  value: unknown,
  maxChars: number = MAX_TARGET_CHARS,
): string {
  if (value === null || value === undefined) return "";
  const singleLine = String(value)
    .replace(/\r\n?/g, "\n")
    .replace(/\n/g, " ")
    .replace(/\t/g, "  ")
    .trim();
  if (singleLine.length <= maxChars) return singleLine;
  if (maxChars <= 1) return "…";
  return singleLine.slice(0, maxChars - 1).trimEnd() + "…";
}

/** Normalize a preview blob into at most MAX_PREVIEW_LINES visible lines. */
export function buildPreviewLines(
  preview: string | null | undefined,
  maxLines: number = MAX_PREVIEW_LINES,
): string[] {
  if (!preview) return [];
  const normalized = String(preview).replace(/\r\n?/g, "\n");
  const lines = normalized.split("\n");
  // Drop a single trailing newline so it does not consume a preview line.
  if (lines.length > 1 && lines[lines.length - 1] === "") {
    lines.pop();
  }
  if (lines.length <= maxLines) return lines;
  return [...lines.slice(0, maxLines), "…"];
}

/** Map a dialog option value to the kernel response payload. */
export function permissionOptionToResult(value: string): PermissionResult {
  if (value === "session") {
    return { approved: true, scope: "session" };
  }
  if (value === "deny") {
    return { approved: false };
  }
  return { approved: true, scope: "once" };
}

/**
 * A guaranteed single rendered line: newline-free input, width-aware
 * truncation with a Unicode ellipsis, padded like pi-tui's Text.
 */
class SingleLineText implements Component {
  constructor(private readonly text: string) {}

  public invalidate(): void {}

  public render(width: number): string[] {
    return [truncateToWidth(this.text, Math.max(1, width), "…", true)];
  }
}

function riskStyle(risk: PermissionRisk | undefined): {
  label: string;
  color: "error" | "warning" | "dim";
} {
  switch (risk) {
    case "high":
      return { label: "[high]", color: "error" };
    case "low":
      return { label: "[low]", color: "dim" };
    case "medium":
    default:
      return { label: "[medium]", color: "warning" };
  }
}

/**
 * Modal-ish permission dialog rendered in the editor viewport.
 *
 * Keyboard: ↑/↓ move, Enter confirm, Esc / Ctrl+C deny.
 */
export class PermissionDialogComponent extends Container {
  public readonly selectList: SelectList;
  private settled = false;

  constructor(
    public readonly request: PermissionRequestParams,
    private readonly onDecision: (result: PermissionResult) => void,
    private readonly requestRender?: () => void,
  ) {
    super();

    const toolName =
      this.request.tool_name || this.request.action || "未知工具";
    const risk = this.request.risk;
    const riskInfo = riskStyle(risk);

    this.addChild(new DynamicBorder());
    this.addChild(new Spacer(1));

    // Title + risk badge. The risk color is driven by the server-provided risk.
    this.addChild(
      new Text(
        `${theme.bold(`需要授权 · ${toolName}`)}  ${theme.fg(riskInfo.color, riskInfo.label)}`,
        0,
        0,
      ),
    );

    this.addChild(new Spacer(1));

    const target = truncateSingleLine(
      this.request.target || this.request.action || "",
    );
    if (target) {
      this.addChild(
        new SingleLineText(
          `${theme.fg("muted", "目标: ")}${theme.fg("text", target)}`,
        ),
      );
    }

    const previewLines = buildPreviewLines(this.request.preview);
    if (previewLines.length > 0) {
      this.addChild(new Spacer(1));
      this.addChild(new Text(theme.fg("muted", "预览:"), 0, 0));
      this.addChild(
        new Text(
          theme.fg("dim", previewLines.map((line) => `  ${line}`).join("\n")),
          0,
          0,
        ),
      );
    }

    if (this.request.reason) {
      this.addChild(new Spacer(1));
      this.addChild(
        new Text(
          theme.fg("warning", `原因: ${this.request.reason}`),
          0,
          0,
        ),
      );
    }

    this.addChild(new Spacer(1));

    const listTheme: SelectListTheme = {
      selectedPrefix: (text: string) => theme.fg("accent", text),
      selectedText: (text: string) => theme.bold(theme.fg("accent", text)),
      description: (text: string) => theme.fg("muted", text),
      scrollInfo: (text: string) => theme.dim(text),
      noMatch: (_text: string) => theme.dim("无可选项"),
    };
    this.selectList = new SelectList(
      [...PERMISSION_OPTIONS],
      PERMISSION_OPTIONS.length,
      listTheme,
      { minPrimaryColumnWidth: 18, maxPrimaryColumnWidth: 24 },
    );
    this.selectList.onSelect = (item: SelectItem) => {
      this.decide(item.value);
    };
    this.selectList.onCancel = () => {
      this.deny();
    };
    this.addChild(this.selectList);

    this.addChild(new Spacer(1));
    this.addChild(
      new Text(
        theme.fg("dim", "  ↑↓ 选择 · Enter 确认 · Esc 拒绝"),
        0,
        0,
      ),
    );
    this.addChild(new Spacer(1));
    this.addChild(new DynamicBorder());
  }

  public render(width: number): string[] {
    try {
      return super.render(width);
    } catch {
      // Never leave the kernel waiting 300s because a child failed to render:
      // fail closed and replace the broken dialog with a small notice.
      this.finish({ approved: false });
      return [
        truncateToWidth(
          theme.fg("error", "⚠ 审批弹窗渲染失败，已自动拒绝本次调用。"),
          Math.max(1, width),
          "…",
          true,
        ),
      ];
    }
  }

  public handleInput(data: string): void {
    if (this.settled) return;

    if (matchesKey(data, "up") || matchesKey(data, "down")) {
      this.selectList.handleInput(data);
      this.requestRender?.();
      return;
    }

    if (isEnterKey(data)) {
      this.selectList.handleInput("\r");
      return;
    }

    if (matchesKey(data, "escape") || matchesKey(data, "ctrl+c")) {
      this.deny();
    }
  }

  /** True once a decision was emitted (prevents double responses). */
  public get isSettled(): boolean {
    return this.settled;
  }

  private deny(): void {
    this.finish({ approved: false });
  }

  private decide(optionValue: string): void {
    this.finish(permissionOptionToResult(optionValue));
  }

  private finish(result: PermissionResult): void {
    if (this.settled) return;
    this.settled = true;
    this.onDecision(result);
  }
}
