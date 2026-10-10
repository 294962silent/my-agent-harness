import { Spacer } from "@earendil-works/pi-tui";
import { AssistantMessageComponent } from "../components/assistant-message.js";
import { ToolExecutionComponent } from "../components/tool-execution.js";
import { UserMessageComponent } from "../components/user-message.js";
import type { MessageContent } from "../protocol.js";
import type { InteractiveMode } from "./interactive-mode.js";

export function displayMessageContent(content: MessageContent): string {
  return typeof content === "string"
    ? content
    : content.map((block) => (block.type === "image" ? "[image]" : block.text)).join("");
}

/**
 * 集中分发处理 Python 内核传来的 ReAct 生命周期事件并驱动组件挂载与状态流转。
 */
export function dispatchAgentEvent(mode: InteractiveMode, event: any): void {
  if (!event || !event.type) return;

  switch (event.type) {
    case "agent_start": {
      mode.isStreaming = true;
      mode.isWorking = true;
      mode.hasRenderedTurnError = false;
      mode.activeToolCalls.clear();
      mode.currentStreamingAssistant = undefined;
      mode.showWorkingStatusIndicator("Working");
      break;
    }

    case "turn_start": {
      mode.isWorking = true;
      mode.hasRenderedTurnError = false;
      mode.showWorkingStatusIndicator("Working");
      break;
    }

    case "message_start": {
      if (event.message?.role === "user") {
        const content = displayMessageContent(event.message.content);
        if (content) {
          let isLocallyPreRendered = false;
          if (mode.pendingSubmittedPrompts > 0) {
            mode.pendingSubmittedPrompts--;
            isLocallyPreRendered = true;
          }

          const steerIdx = mode.pendingSteeringList.findIndex(
            (message) => displayMessageContent(message) === content,
          );
          if (steerIdx !== -1) {
            mode.pendingSteeringList.splice(steerIdx, 1);
          } else {
            const followIdx = mode.pendingFollowupList.findIndex(
              (message) => displayMessageContent(message) === content,
            );
            if (followIdx !== -1) {
              mode.pendingFollowupList.splice(followIdx, 1);
            }
          }
          mode.updatePendingMessagesDisplay();

          // 若不是刚刚在 handleUserInput 中本地预挂载的普通 Prompt，则正常挂载用户气泡
          if (!isLocallyPreRendered) {
            const userMsg = new UserMessageComponent(content);
            mode.chatContainer.addChild(userMsg);
            mode.chatContainer.addChild(new Spacer(1));
          }
        }
      } else if (event.message?.role === "assistant") {
        mode.currentStreamingAssistant = new AssistantMessageComponent();
        mode.chatContainer.addChild(mode.currentStreamingAssistant);
        mode.chatContainer.addChild(new Spacer(1));
      }
      break;
    }

    case "message_update": {
      if (!mode.currentStreamingAssistant) {
        mode.currentStreamingAssistant = new AssistantMessageComponent();
        mode.latestAssistantMessage = mode.currentStreamingAssistant;
        mode.chatContainer.addChild(mode.currentStreamingAssistant);
        mode.chatContainer.addChild(new Spacer(1));
      }

      // 解析 message.content 中的 thinking 与 text 块 (使用全量快照，杜绝二次方爆炸)
      if (Array.isArray(event.message?.content)) {
        let thinkingText = "";
        let contentText = "";
        for (const block of event.message.content) {
          if (block.type === "thinking" && block.thinking) {
            thinkingText += block.thinking;
          } else if (block.type === "text" && block.text) {
            contentText += block.text;
          }
        }
        if (thinkingText) {
          mode.currentStreamingAssistant.setReasoning(thinkingText);
        }
        if (contentText) {
          mode.currentStreamingAssistant.setContent(contentText);
        }
      } else if (
        typeof event.message?.content === "string" &&
        event.message.content
      ) {
        mode.currentStreamingAssistant.setContent(event.message.content);
      }
      break;
    }

    case "message_end": {
      if (event.message?.role !== "assistant") break;
      const isError =
        event.message?.metadata?.stop_reason === "error" ||
        event.message?.stopReason === "error" ||
        event.message?.role === "error" ||
        event.stop_reason === "error" ||
        event.stopReason === "error";

      const contentStr =
        typeof event.message?.content === "string"
          ? event.message.content
          : Array.isArray(event.message?.content)
            ? event.message.content
                .map((b: any) =>
                  typeof b === "string" ? b : b?.text || b?.thinking || "",
                )
                .join("")
            : String(event.message?.content ?? "");

      if (mode.currentStreamingAssistant) {
        const isCancelled =
          event.message?.metadata?.stop_reason === "cancelled" ||
          event.message?.metadata?.stop_reason === "aborted" ||
          event.message?.stopReason === "cancelled" ||
          event.message?.stopReason === "aborted";
        if (isCancelled) {
          mode.chatContainer.removeChild(mode.currentStreamingAssistant);
          mode.currentStreamingAssistant = undefined;
          break;
        }
        if (!mode.currentStreamingAssistant.getContentText() && contentStr) {
          if (isError) {
            mode.chatContainer.removeChild(mode.currentStreamingAssistant);
            mode.appendErrorMessage(contentStr);
            mode.hasRenderedTurnError = true;
          } else {
            mode.currentStreamingAssistant.setContent(contentStr);
          }
        } else if (isError && contentStr) {
          mode.appendErrorMessage(contentStr);
          mode.hasRenderedTurnError = true;
        }
        mode.currentStreamingAssistant.finalize();
        mode.latestAssistantMessage = mode.currentStreamingAssistant;
        mode.currentStreamingAssistant = undefined;
      } else if (contentStr && event.message?.role === "assistant") {
        const isCancelled =
          event.message?.metadata?.stop_reason === "cancelled" ||
          event.message?.metadata?.stop_reason === "aborted" ||
          event.message?.stopReason === "cancelled" ||
          event.message?.stopReason === "aborted";
        if (isCancelled) {
          // 中断废弃的半截文本直接忽略，不新建气泡挂载
          break;
        }
        if (isError) {
          mode.appendErrorMessage(contentStr);
          mode.hasRenderedTurnError = true;
        } else {
          const assistant = new AssistantMessageComponent();
          assistant.setContent(contentStr);
          assistant.finalize();
          mode.chatContainer.addChild(assistant);
          mode.chatContainer.addChild(new Spacer(1));
          mode.latestAssistantMessage = assistant;
        }
      }
      if (event.usage) {
        mode.updateFooterUsage(event.usage, event.contextWindow);
      }
      break;
    }

    case "tool_execution_start": {
      const id = event.toolCallId || `tc-${Date.now()}`;
      const name = event.toolName || "tool";
      const args = event.args || {};
      const toolComponent = new ToolExecutionComponent(name, id, args, () =>
        mode.ui.requestRender(),
      );
      mode.activeToolCalls.set(id, toolComponent);
      mode.toolStartTimes.set(id, Date.now());
      mode.chatContainer.addChild(toolComponent);
      mode.chatContainer.addChild(new Spacer(1));
      mode.showWorkingStatusIndicator(`正在执行工具: ${name}...`);
      break;
    }

    case "tool_execution_update": {
      const id = event.toolCallId;
      const toolComponent = mode.activeToolCalls.get(id);
      if (toolComponent && event.partialResult) {
        toolComponent.updatePartialResult(event.partialResult);
      }
      break;
    }

    case "tool_execution_end": {
      const id = event.toolCallId || event.tool_call_id;
      const toolComponent = id ? mode.activeToolCalls.get(id) : undefined;
      if (toolComponent) {
        toolComponent.updateResult(event.result, Boolean(event.isError));
        mode.toolStartTimes.delete(id);
      }
      if (mode.isWorking) {
        mode.showWorkingStatusIndicator("Working");
      }
      break;
    }

    case "turn_end": {
      mode.isWorking = false;
      mode.clearStatusDisplay();
      if (event.usage) {
        mode.updateFooterUsage(event.usage, event.contextWindow);
      }
      break;
    }

    case "agent_end": {
      try {
        mode.isStreaming = false;
        mode.isWorking = false;
        mode.isSubmitting = false;
        mode.pendingSteeringList = [];
        mode.pendingFollowupList = [];
        mode.updatePendingMessagesDisplay();
        const stopReason = String(event.stop_reason ?? event.stopReason ?? "");
        const isError = stopReason === "error";
        const isCancelled =
          stopReason === "cancelled" || stopReason === "aborted";
        const finalText = String(event.final_text ?? event.finalText ?? "");

        if (mode.currentStreamingAssistant) {
          if (
            isCancelled ||
            (!mode.currentStreamingAssistant.getContentText() &&
              finalText &&
              isError)
          ) {
            mode.chatContainer.removeChild(mode.currentStreamingAssistant);
          } else {
            mode.currentStreamingAssistant.finalize();
          }
          mode.currentStreamingAssistant = undefined;
        }
        // 自动闭合所有未正常结束的工具调用
        for (const tool of mode.activeToolCalls.values()) {
          if (!tool.finished) {
            tool.updateResult("执行中断", true);
          }
        }
        mode.activeToolCalls.clear();
        mode.toolStartTimes.clear();
        if (event.usage) {
          mode.updateFooterUsage(event.usage, event.contextWindow);
        }
        if (isError && finalText && !mode.hasRenderedTurnError) {
          mode.appendErrorMessage(finalText);
          mode.hasRenderedTurnError = true;
        }
      } finally {
        mode.clearStatusDisplay();
        mode.footer.update({ isBusy: false });
      }
      break;
    }

    case "context_compacted": {
      if (event.tokensAfter !== undefined) {
        mode.footer.update({ contextTokens: event.tokensAfter });
      }
      mode.appendSystemNotice(
        `✓ 上下文已压缩: ${(event.tokensBefore ?? 0).toLocaleString()} -> ${(event.tokensAfter ?? 0).toLocaleString()} tokens`,
      );
      break;
    }

    case "auto_retry_start": {
      const sec = ((event.delayMs || 2000) / 1000).toFixed(1);
      const err = event.errorMessage ? `: ${event.errorMessage}` : "";
      const text = `重试中 (${event.attempt}/${event.maxAttempts}) ${sec}s 后继续${err} (Esc 取消)`;
      mode.showWorkingStatusIndicator(text);
      break;
    }

    case "auto_retry_end": {
      if (event.success) {
        mode.showWorkingStatusIndicator("Working");
      } else {
        mode.clearStatusDisplay();
        if (event.finalError) {
          mode.appendErrorMessage(
            `已达最大重试次数 (${event.attempt})，请求失败: ${event.finalError}`,
          );
          mode.hasRenderedTurnError = true;
        }
      }
      break;
    }
  }

  mode.ui.requestRender();
}
