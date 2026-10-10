import { Spacer, Text } from "@earendil-works/pi-tui";
import { AssistantMessageComponent } from "../components/assistant-message.js";
import { CompactionSummaryMessageComponent } from "../components/compaction-summary-message.js";
import { ToolExecutionComponent } from "../components/tool-execution.js";
import { UserMessageComponent } from "../components/user-message.js";
import { formatTokens } from "../components/footer.js";
import { theme } from "../theme/theme.js";
import type { InteractiveMode } from "./interactive-mode.js";

/**
 * 渲染指定会话的历史转录本消息列表到 chatContainer 中。
 */
export function renderSessionHistory(
  mode: InteractiveMode,
  messages: any[],
  banner?: string,
): void {
  mode.chatContainer.clear();
  mode.activeToolCalls.clear();
  mode.toolStartTimes.clear();
  mode.currentStreamingAssistant = undefined;

  if (banner) {
    const bannerComp = new AssistantMessageComponent();
    bannerComp.appendTextDelta(banner);
    bannerComp.finalize();
    mode.chatContainer.addChild(bannerComp);
  } else if (!messages || messages.length === 0) {
    const welcome = new Text(
      theme.fg("muted", "欢迎使用 my-pi-agent！输入需求或按 / 开启命令菜单。"),
      1,
      0,
    );
    mode.chatContainer.addChild(welcome);
  }

  if (!messages || messages.length === 0) {
    mode.ui.requestRender();
    return;
  }

  const pendingTools = new Map<string, ToolExecutionComponent>();

  for (const msg of messages) {
    if (
      msg.role === "compaction" ||
      msg.type === "compaction" ||
      msg.role === "compactionSummary"
    ) {
      const compComponent = new CompactionSummaryMessageComponent({
        summary: String(msg.summary || msg.content || ""),
        tokensBefore: Number(msg.tokens_before || msg.tokensBefore || 0),
      });
      mode.chatContainer.addChild(new Spacer(1));
      mode.chatContainer.addChild(compComponent);
      continue;
    }
    if (msg.role === "user") {
      const userText =
        typeof msg.content === "string"
          ? msg.content
          : Array.isArray(msg.content)
            ? msg.content
                .map((b: any) =>
                  typeof b === "string" ? b : b?.type === "image" ? "[image]" : b?.text || b?.content || "",
                )
                .join("")
            : String(msg.content ?? "");
      mode.chatContainer.addChild(new UserMessageComponent(userText));
    } else if (msg.role === "assistant") {
      const assistantComp = new AssistantMessageComponent();
      const thinking = msg.metadata?.thinking || msg.metadata?.reasoning_content;
      if (thinking) {
        assistantComp.setReasoning(String(thinking));
      }
      const assistantText =
        typeof msg.content === "string"
          ? msg.content
          : Array.isArray(msg.content)
            ? msg.content
                .map((b: any) =>
                  typeof b === "string" ? b : b?.text || b?.content || "",
                )
                .join("")
            : msg.content == null
              ? ""
              : String(msg.content);
      if (assistantText) {
        assistantComp.setContent(assistantText);
      }
      assistantComp.finalize();
      mode.chatContainer.addChild(assistantComp);

      const toolCalls = msg.metadata?.tool_calls;
      if (Array.isArray(toolCalls)) {
        for (const tc of toolCalls) {
          const rawTc = tc as Record<string, unknown>;
          const toolName = String(
            rawTc.name ||
              (rawTc.function as Record<string, unknown>)?.name ||
              "tool",
          );
          const callId = String(rawTc.id || "");
          let parsedArgs: Record<string, unknown> = {};
          if (rawTc.args && typeof rawTc.args === "object") {
            parsedArgs = rawTc.args as Record<string, unknown>;
          } else if ((rawTc.function as Record<string, unknown>)?.arguments) {
            const fnArgs = (rawTc.function as Record<string, unknown>).arguments;
            if (typeof fnArgs === "string") {
              try {
                parsedArgs = JSON.parse(fnArgs);
              } catch {
                parsedArgs = { raw: fnArgs };
              }
            } else if (typeof fnArgs === "object" && fnArgs !== null) {
              parsedArgs = fnArgs as Record<string, unknown>;
            }
          } else if (rawTc.arguments && typeof rawTc.arguments === "object") {
            parsedArgs = rawTc.arguments as Record<string, unknown>;
          }
          const toolComp = new ToolExecutionComponent(
            toolName,
            callId,
            parsedArgs,
          );
          mode.chatContainer.addChild(toolComp);
          if (callId) {
            pendingTools.set(callId, toolComp);
          }
        }
      }
    } else if (msg.role === "tool") {
      const callId = String(msg.metadata?.tool_call_id || "");
      const toolComp = callId ? pendingTools.get(callId) : undefined;
      const isError = Boolean(msg.metadata?.is_error);
      if (toolComp) {
        toolComp.updateResult(msg.content, isError);
        pendingTools.delete(callId);
      } else {
        const toolName = String(msg.metadata?.tool_name || "tool");
        const standalone = new ToolExecutionComponent(toolName, callId, {});
        standalone.updateResult(msg.content, isError);
        mode.chatContainer.addChild(standalone);
      }
    }
  }

  for (const toolComp of pendingTools.values()) {
    if (!toolComp.finished) {
      toolComp.updateResult("(已完成)", false);
    }
  }

  mode.transcriptScrollView?.scrollToEnd?.();
  mode.ui.requestRender();
}

/**
 * 对标 Pi 原厂 /session 统计输出，格式化展示会话全局 Token、命中率与各模型花费账单。
 */
export function renderPiSessionStats(mode: InteractiveMode, stats: any): void {
  let info = `${theme.bold("Session Info")}\n\n`;
  const sessionName =
    stats.sessionName || (mode.footer as any)?.data?.sessionName;
  if (sessionName) {
    info += `${theme.fg("dim", "Name:")} ${sessionName}\n`;
  }
  info += `${theme.fg("dim", "File:")}\n${stats.sessionFile ?? "In-memory"}\n`;
  info += `${theme.fg("dim", "ID:")} ${stats.sessionId}\n\n`;

  info += `${theme.bold("Messages")}\n`;
  info += `${theme.fg("dim", "Total:")} ${stats.totalMessages ?? 0}\n`;
  info += `${theme.fg("dim", "User:")} ${stats.userMessages ?? 0}\n`;
  info += `${theme.fg("dim", "Assistant:")} ${stats.assistantMessages ?? 0}\n`;
  info += `${theme.fg("dim", "Tools:")} ${stats.toolCalls ?? 0} calls, ${stats.toolResults ?? 0} results\n\n`;

  info += `${theme.bold("Tokens")}\n`;
  const tokens = stats.tokens || {};
  const input = Number(tokens.input ?? 0);
  const cacheRead = Number(tokens.cacheRead ?? 0);
  const cacheWrite = Number(tokens.cacheWrite ?? 0);
  const output = Number(tokens.output ?? 0);
  const promptTokens = input + cacheRead + cacheWrite;
  const total = Number(tokens.total ?? promptTokens + output);

  info += `${theme.fg("dim", "Input:")} ${promptTokens.toLocaleString()}\n`;
  if (promptTokens > 0 && (cacheRead > 0 || cacheWrite > 0)) {
    const hitRate = theme.fg(
      "dim",
      `(${((cacheRead / promptTokens) * 100).toFixed(1)}%)`,
    );
    info += `  ${theme.fg("dim", "Cached:")} ${cacheRead.toLocaleString()} ${hitRate}\n`;
    const written =
      cacheWrite > 0
        ? ` ${theme.fg("dim", `(${cacheWrite.toLocaleString()} written to cache)`)}`
        : "";
    info += `  ${theme.fg("dim", "Uncached:")} ${(input + cacheWrite).toLocaleString()}${written}\n`;
  }
  info += `${theme.fg("dim", "Output:")} ${output.toLocaleString()}\n`;
  info += `${theme.fg("dim", "Total:")} ${total.toLocaleString()}\n`;

  const cost = Number(stats.cost ?? 0);
  const breakdown = stats.usageBreakdown || [];
  const cacheWaste = stats.cacheWaste || {};
  if (cost > 0 || (cacheWaste.missedTokens && cacheWaste.missedTokens > 0)) {
    info += `\n${theme.bold("Cost")}\n`;
    info += `${theme.fg("dim", "Total:")} $${cost.toFixed(3)}`;
    if (breakdown.length > 0) {
      for (const entry of breakdown) {
        info += `\n  ${theme.fg("dim", `${entry.key}:`)} $${Number(entry.cost ?? 0).toFixed(3)} ${theme.fg("dim", `(${formatTokens(entry.tokens ?? 0)} tokens)`)}`;
      }
    }
    if (cacheWaste.missedTokens > 0) {
      const missLabel =
        cacheWaste.missCount === 1
          ? "1 miss"
          : `${cacheWaste.missCount} misses`;
      const detail = `${cacheWaste.missedTokens.toLocaleString()} tokens, ${missLabel}`;
      info +=
        cacheWaste.missedCost >= 0.0001
          ? `\n${theme.fg("dim", "Cache Re-billed:")} $${cacheWaste.missedCost.toFixed(3)} ${theme.fg("dim", `(${detail})`)}`
          : `\n${theme.fg("dim", "Cache Re-billed:")} ${detail}`;
    }
  }

  mode.chatContainer.addChild(new Spacer(1));
  mode.chatContainer.addChild(new Text(info, 1, 0));
  mode.ui.requestRender();
}

/**
 * 降级模式的简要统计信息输出。
 */
export function renderFallbackSessionStats(mode: InteractiveMode): void {
  const sessionName = (mode.footer as any)?.data?.sessionName || "default";
  const model = mode.currentModelName;
  const thinking = mode.currentThinkingLevel;
  const messageCount = mode.chatContainer.children.length;
  const info = [
    theme.bold("Session Info"),
    "",
    `  ${theme.fg("dim", "Name:")} ${sessionName}`,
    `  ${theme.fg("dim", "Workspace:")} ${mode.workspace}`,
    `  ${theme.fg("dim", "Model:")} ${model} (thinking: ${thinking})`,
    `  ${theme.fg("dim", "Messages:")} ${messageCount}`,
  ].join("\n");
  mode.chatContainer.addChild(new Spacer(1));
  mode.chatContainer.addChild(new Text(info, 1, 0));
  mode.ui.requestRender();
}
