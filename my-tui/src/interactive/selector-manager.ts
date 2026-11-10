import {
  ModelSelectorComponent,
  type ModelItem,
} from "../components/model-selector.js";
import {
  SessionSelectorComponent,
  type SessionItem,
} from "../components/session-selector.js";
import { ThinkingSelectorComponent } from "../components/thinking-selector.js";
import { LoginSelectorComponent } from "../components/login-selector.js";
import {
  LogoutSelectorComponent,
  type LogoutProviderItem,
} from "../components/logout-selector.js";
import { ThemeSelectorComponent } from "../components/theme-selector.js";
import {
  TreeSelectorComponent,
  type TreeNode,
} from "../components/tree-selector.js";
import {
  UserMessageSelectorComponent,
  type UserMessageItem,
} from "../components/user-message-selector.js";
import { SettingsSelectorComponent } from "../components/settings-selector.js";
import { PermissionDialogComponent } from "../components/permission-dialog.js";
import type { PermissionRequestParams, PermissionResult } from "../client.js";
import { theme } from "../theme/theme.js";
import type { InteractiveMode } from "./interactive-mode.js";

/**
 * 唤起工具审批弹窗 (Permission Approval)
 *
 * 弹窗做出选择后先卸载自身（焦点交还编辑器），再把结果回传给调用方。
 * 构造函数若抛异常会向上冒泡，由调用方兜底回 { approved: false }。
 */
export function openPermissionDialog(
  mode: InteractiveMode,
  request: PermissionRequestParams,
  onDecision: (result: PermissionResult) => void,
): void {
  mode.showSelector((done) => {
    const dialog = new PermissionDialogComponent(
      request,
      (result: PermissionResult) => {
        done();
        onDecision(result);
      },
      () => mode.ui.requestRender(),
    );
    return { component: dialog, focus: dialog };
  });
}

/**
 * 唤起模型选择器 (Model Catalog)
 */
export function openModelSelector(mode: InteractiveMode, initialSearch?: string): void {
  mode.showSelector((done) => {
    const selector = new ModelSelectorComponent(
      mode.currentModelName,
      async () => {
        const res = await mode.bridge.listModels({ scope: "configured" });
        return ((res as any)?.models || []).map((m: any) => ({
          id: m.id || m.name,
          name: m.name || m.id,
          provider: m.provider || "default",
          contextWindow: m.contextWindow || m.context_window || 128000,
          is_configured: m.is_configured ?? true,
        }));
      },
      async (selected: ModelItem) => {
        done();
        if (selected) {
          try {
            mode.currentModelName = selected.id;
            const supported = mode.getSupportedThinkingLevels();
            if (!supported.includes(mode.currentThinkingLevel.toLowerCase())) {
              mode.currentThinkingLevel = supported[0] || "off";
              void mode.bridge.setThinking(mode.currentThinkingLevel);
            }
            const ctxWin =
              selected.contextWindow ||
              (selected.id.startsWith("gemini-") ? 1048576 : 128000);
            mode.footer.update({
              modelName: selected.id,
              providerName: selected.provider,
              thinkingLevel: mode.currentThinkingLevel,
              contextWindow: ctxWin,
            });
            mode.updateEditorBorderColor();
            const switchRes: any = await mode.bridge.switchModel(
              selected.id,
              selected.provider,
            );
            if (switchRes?.context_window || switchRes?.contextWindow) {
              mode.footer.update({
                contextWindow:
                  switchRes.context_window || switchRes.contextWindow,
              });
            }
            mode.appendSystemNotice(
              `✓ 已成功切换至模型: ${selected.id} (${selected.provider})`,
            );
          } catch (err: any) {
            mode.appendErrorMessage(
              `切换模型失败: ${err.message || String(err)}`,
            );
          }
        }
      },
      () => done(),
      initialSearch,
      async (defaultModel: ModelItem) => {
        done();
        if (defaultModel) {
          try {
            mode.currentModelName = defaultModel.id;
            const supported = mode.getSupportedThinkingLevels();
            if (!supported.includes(mode.currentThinkingLevel.toLowerCase())) {
              mode.currentThinkingLevel = supported[0] || "off";
              void mode.bridge.setThinking(mode.currentThinkingLevel);
            }
            const ctxWin =
              defaultModel.contextWindow ||
              (defaultModel.id.startsWith("gemini-") ? 1048576 : 128000);
            mode.footer.update({
              modelName: defaultModel.id,
              providerName: defaultModel.provider,
              thinkingLevel: mode.currentThinkingLevel,
              contextWindow: ctxWin,
            });
            mode.updateEditorBorderColor();
            const switchRes: any = await mode.bridge.switchModel(
              defaultModel.id,
              defaultModel.provider,
            );
            if (switchRes?.context_window || switchRes?.contextWindow) {
              mode.footer.update({
                contextWindow:
                  switchRes.context_window || switchRes.contextWindow,
              });
            }
            await mode.bridge.setSetting("defaultModel", defaultModel.id);
            await mode.bridge.setSetting(
              "defaultProvider",
              defaultModel.provider,
            );
            mode.appendSystemNotice(
              `✓ 已成功切换并保存为默认模型: ${defaultModel.id} (${defaultModel.provider})`,
            );
          } catch (err: any) {
            mode.appendErrorMessage(
              `设置默认模型失败: ${err.message || String(err)}`,
            );
          }
        }
      },
      undefined,
      () => mode.ui.requestRender(),
    );
    return { component: selector, focus: selector };
  });
}

/**
 * 唤起会话切换与管理选择器 (Session Selector)
 */
export function openSessionSelector(mode: InteractiveMode): void {
  mode.showSelector((done) => {
    const selector = new SessionSelectorComponent(
      async (allProjects: boolean) => {
        const res = await mode.bridge.listSessions(
          allProjects ? { all_projects: true } : {},
        );
        return ((res as any)?.sessions || []).map((s: any) => ({
          id: s.id || s.session_id,
          name: s.name || s.title || s.first_message || s.session_id,
          path: s.path,
          modified:
            s.modified ??
            (s.updated_at
              ? Math.floor(s.updated_at / 1000)
              : Math.floor(Date.now() / 1000)),
          cwd: s.cwd || s.workspace || mode.workspace,
          message_count: s.message_count || 0,
          parent_session: s.parent_session || s.parent_session_path,
          parent_session_path: s.parent_session_path || s.parent_session,
        }));
      },
      async (session: SessionItem) => {
        done();
        if (session) {
          try {
            const target = session.path || session.id;
            const res: any = await mode.bridge.resumeSession(target);
            if (res?.session_name || res?.session_id) {
              mode.footer.update({
                sessionName: res.session_name || res.session_id,
              });
            }
            if (res?.usage) {
              mode.updateFooterUsage(
                res.usage,
                res.contextWindow ?? res.context_window,
              );
            }
            mode.renderSessionHistory(
              res?.messages || [],
              `✓ 已成功恢复会话: \`${res?.session_name || res?.session_id || session.id}\``,
            );
          } catch (err: any) {
            mode.appendErrorMessage(
              `恢复会话失败: ${err.message || String(err)}`,
            );
          }
        }
      },
      () => done(),
      () => mode.ui.requestRender(),
      mode.footer.getSessionName(),
      async (sessionToDelete: SessionItem) => {
        try {
          await mode.bridge.deleteSession(
            sessionToDelete.path || sessionToDelete.id,
          );
          mode.appendSystemNotice(
            `✓ 已成功删除历史会话: \`${sessionToDelete.name || sessionToDelete.id}\``,
          );
        } catch (err: unknown) {
          const msg = err instanceof Error ? err.message : String(err);
          mode.appendErrorMessage(`删除会话失败: ${msg}`);
        }
      },
    );
    return { component: selector, focus: selector };
  });
}

/**
 * 唤起思考预算深度选择器 (Thinking Budget Selector)
 */
export function openThinkingSelector(mode: InteractiveMode): void {
  const levels = mode.getSupportedThinkingLevels();
  if (levels.length === 1 && levels[0] === "off") {
    mode.appendSystemNotice("当前模型不支持思考模式 (thinking: off)。");
    return;
  }
  mode.showSelector((done) => {
    const selector = new ThinkingSelectorComponent(
      mode.currentThinkingLevel,
      levels,
      async (level: string) => {
        done();
        if (level) {
          try {
            mode.currentThinkingLevel = level;
            mode.footer.update({ thinkingLevel: level });
            mode.updateEditorBorderColor();
            await mode.bridge.setThinking(level);
            mode.appendSystemNotice(`✓ 思考预算等级已调整为: ${level}`);
          } catch (err: any) {
            mode.appendErrorMessage(
              `设置思考预算失败: ${err.message || String(err)}`,
            );
          }
        }
      },
      () => done(),
    );
    return { component: selector, focus: selector };
  });
}

/**
 * 唤起 API 认证登录面板 (Login Selector)
 */
export function openLoginSelector(mode: InteractiveMode): void {
  mode.showSelector((done) => {
    const selector = new LoginSelectorComponent(
      async (provider: string, key: string) => {
        done();
        try {
          await mode.bridge.login(provider, key);
          mode.appendSystemNotice(`✓ 已成功为 ${provider} 绑定 API 密钥。`);
        } catch (err: any) {
          mode.appendErrorMessage(
            `绑定 API 密钥失败: ${err.message || String(err)}`,
          );
        }
      },
      () => done(),
    );
    return { component: selector, focus: selector };
  });
}

/**
 * 唤起 API 凭据注销面板 (Logout Selector)
 */
export function openLogoutSelector(mode: InteractiveMode): void {
  mode.showSelector((done) => {
    const defaultProviders: LogoutProviderItem[] = [
      { id: "deepseek", label: "DeepSeek", description: "deepseek API key" },
      { id: "openai", label: "OpenAI", description: "openai API key" },
      { id: "anthropic", label: "Anthropic", description: "anthropic API key" },
      { id: "antigravity", label: "Antigravity", description: "oauth credential" },
    ];
    const selector = new LogoutSelectorComponent(
      defaultProviders,
      async (providerId: string) => {
        done();
        try {
          await mode.bridge.logout(providerId);
          mode.appendSystemNotice(`✓ 已成功注销 ${providerId} 的凭据。`);
        } catch (err: any) {
          mode.appendErrorMessage(
            `注销凭据失败: ${err.message || String(err)}`,
          );
        }
      },
      () => done(),
    );
    return { component: selector, focus: selector };
  });
}

/**
 * 唤起终端配色主题选择器 (Theme Selector)
 */
export function openThemeSelector(mode: InteractiveMode): void {
  const curTheme = theme.currentThemeName;
  mode.showSelector((done) => {
    const selector = new ThemeSelectorComponent(
      curTheme,
      ["dark", "light"],
      async (themeName: string) => {
        done();
        theme.setTheme(themeName);
        try {
          await mode.bridge.setSetting("theme", themeName);
        } catch {
          // ignore
        }
        mode.appendSystemNotice(`✓ 主题已切换至: ${themeName}`);
        mode.ui.requestRender();
      },
      () => {
        theme.setTheme(curTheme);
        done();
        mode.ui.requestRender();
      },
      (previewTheme: string) => {
        theme.setTheme(previewTheme);
        mode.ui.requestRender();
      },
    );
    return { component: selector, focus: selector };
  });
}

/**
 * 唤起会话 DAG 拓扑分支树选择器 (Tree Selector)
 */
export function openTreeSelector(mode: InteractiveMode): void {
  mode.showSelector((done) => {
    const selector = new TreeSelectorComponent(
      async () => {
        const res = await mode.bridge.getTree();
        return ((res as any)?.nodes || (res as any)?.tree || []) as TreeNode[];
      },
      async (node: TreeNode) => {
        done();
        try {
          const branchRes: any = await mode.bridge.branchSession(node.id);
          if (branchRes?.messages && Array.isArray(branchRes.messages)) {
            mode.renderSessionHistory(branchRes.messages);
          }
          if (branchRes?.editor_text) {
            mode.defaultEditor.setMessageContent(branchRes.editor_text);
          }
          mode.appendSystemNotice(`✓ 已切换至分支节点: ${node.id}`);
        } catch (err: any) {
          mode.appendErrorMessage(
            `切换分支失败: ${err.message || String(err)}`,
          );
        }
      },
      () => done(),
      undefined,
      () => mode.ui.requestRender(),
    );
    return { component: selector, focus: selector };
  });
}

/**
 * 唤起历史消息分叉选择器 (Fork Selector)
 */
export async function openForkSelector(mode: InteractiveMode): Promise<void> {
  const userMessages: UserMessageItem[] = [];
  try {
    const res: any = await mode.bridge.getTree();
    const tree: TreeNode[] = (res?.tree || res?.nodes || []) as TreeNode[];
    for (const n of tree) {
      if (n.role === "user") {
        userMessages.push({ id: n.id, text: n.preview || n.id });
      }
    }
  } catch {
    // ignore
  }

  if (userMessages.length === 0) {
    mode.appendSystemNotice("当前会话暂无历史用户消息可供分叉。");
    return;
  }

  mode.showSelector((done) => {
    const selector = new UserMessageSelectorComponent(
      userMessages,
      async (msg: UserMessageItem) => {
        done();
        try {
          const client = (mode.bridge as any).client;
          const res: any =
            (await client?.sendRequest?.("session_fork", { entry_id: msg.id })) ||
            (await (mode.bridge as any).forkSession?.(msg.id));
          if (res?.messages) {
            if (res?.new_session_id) {
              mode.footer.update({ sessionName: res.new_session_id });
            }
            mode.renderSessionHistory(
              res.messages,
              `✓ 已从用户提问分叉开辟新会话: ${res.new_session_id || msg.id}`,
            );
          } else {
            mode.appendSystemNotice(
              `✓ 已成功从节点 ${msg.id} 分叉开辟新会话: ${res?.new_session_id || ""}`,
            );
          }
        } catch (err: any) {
          mode.appendErrorMessage(
            `分叉会话失败: ${err.message || String(err)}`,
          );
        }
      },
      () => done(),
    );
    return { component: selector, focus: selector };
  });
}

/**
 * 唤起可视化设置选择器 (Settings Selector)
 */
export async function openSettingsSelector(mode: InteractiveMode): Promise<void> {
  let currentSettings: Record<string, unknown> = {};
  try {
    const res: any = await mode.bridge.getSettings();
    if (res?.settings) {
      currentSettings = res.settings;
    }
  } catch {
    // ignore
  }

  mode.showSelector((done) => {
    const selector = new SettingsSelectorComponent(
      currentSettings,
      async (key: string, value: unknown) => {
        try {
          await mode.bridge.setSetting(key, value);
        } catch (err: any) {
          mode.appendErrorMessage(
            `修改配置失败: ${err.message || String(err)}`,
          );
        }
      },
      () => done(),
    );
    return { component: selector, focus: selector };
  });
}
