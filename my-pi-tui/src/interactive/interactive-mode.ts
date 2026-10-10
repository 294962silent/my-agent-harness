import * as fs from "node:fs";
import * as path from "node:path";
import * as process from "node:process";
import {
  CombinedAutocompleteProvider,
  type Component,
  Container,
  type EditorTheme,
  matchesKey,
  type SlashCommand,
  Spacer,
  Text,
  TuiMainScreen,
} from "@earendil-works/pi-tui";
import { KernelBridge } from "../bridge/kernel-bridge.js";
import { AssistantMessageComponent } from "../components/assistant-message.js";
import { CompactionSummaryMessageComponent } from "../components/compaction-summary-message.js";
import { CustomEditor } from "../components/custom-editor.js";
import type { MessageContent, MessageContentBlock } from "../protocol.js";
import { DynamicBorder } from "../components/dynamic-border.js";
import { FooterComponent, formatTokens } from "../components/footer.js";
import { HeaderComponent } from "../components/header.js";
import {
  StatusIndicator,
  WorkingStatusIndicator,
} from "../components/status-indicator.js";
import {
  type LoadedResourcesData,
  StartupResourcesComponent,
} from "../components/startup-resources.js";
import { ToolExecutionComponent } from "../components/tool-execution.js";
import { UserMessageComponent } from "../components/user-message.js";
import {
  openForkSelector,
  openLoginSelector,
  openLogoutSelector,
  openModelSelector,
  openSessionSelector,
  openSettingsSelector,
  openThemeSelector,
  openThinkingSelector,
  openTreeSelector,
} from "./selector-manager.js";
import {
  renderFallbackSessionStats,
  renderPiSessionStats,
  renderSessionHistory,
} from "./history-renderer.js";
import {
  dispatchAgentEvent,
  displayMessageContent,
} from "./event-dispatcher.js";
import { theme } from "../theme/theme.js";
import { createChatViewport } from "./chat-viewport.js";
import {
  createInteractiveTui,
  type InteractiveTuiOptions,
} from "./tui-renderer.js";

export interface InteractiveModeOptions extends InteractiveTuiOptions {
  workspace?: string;
  model?: string;
  sessionName?: string;
  thinking?: string;
  noSession?: boolean;
  continueSession?: boolean;
  resume?: string | boolean;
  resources?: LoadedResourcesData;
  debug?: boolean;
}

import {
  BUILTIN_SLASH_COMMANDS,
  executeSlashCommand,
} from "./slash-commands.js";

export { BUILTIN_SLASH_COMMANDS };

function findFdPath(): string | undefined {
  const envPath = process.env.PATH || "";
  const paths = envPath.split(path.delimiter);
  const isWindows = process.platform === "win32";
  const names = isWindows ? ["fd.exe", "fdfind.exe"] : ["fd", "fdfind"];

  for (const dir of paths) {
    for (const name of names) {
      const fullPath = path.join(dir, name);
      try {
        if (fs.existsSync(fullPath)) {
          return fullPath;
        }
      } catch {
        // ignore
      }
    }
  }
  return undefined;
}

export class InteractiveMode {
  public readonly ui: TuiMainScreen;
  public readonly chatContainer: Container;
  public readonly documentContainer: Container;
  public readonly pendingMessagesContainer: Container;
  public readonly statusContainer: Container;
  public readonly editorContainer: Container;
  public readonly defaultEditor: CustomEditor;
  public readonly footer: FooterComponent;
  public readonly header: HeaderComponent;
  public readonly dynamicBorder: DynamicBorder;

  private activeSelectorToken?: object;
  private activeSelectorDispose?: () => void;
  public activeSelectorComponent?: any;

  public currentStreamingAssistant?: AssistantMessageComponent;
  public latestAssistantMessage?: AssistantMessageComponent;
  public activeToolCalls = new Map<string, ToolExecutionComponent>();
  public toolStartTimes = new Map<string, number>();
  public transcriptScrollView?: any;
  public isStreaming = false;
  public isWorking = false;
  public isSubmitting = false;
  public pendingSubmittedPrompts = 0;
  public pendingSteeringList: MessageContent[] = [];
  public pendingFollowupList: MessageContent[] = [];
  public currentThinkingLevel = "off";
  public currentModelName = "default";
  public workspace: string;
  public onExit?: () => Promise<void> | void;
  private unsubscribeBridge?: () => void;
  public hasRenderedTurnError = false;
  public startupResources: StartupResourcesComponent;
  public loadedResources: LoadedResourcesData;

  constructor(
    public readonly bridge: KernelBridge,
    public readonly options: InteractiveModeOptions = {},
  ) {
    this.workspace = options.workspace || process.cwd();
    this.currentModelName = options.model || "default";
    this.currentThinkingLevel = options.thinking || "off";
    this.loadedResources = options.resources || {
      context: [],
      skills: [],
      prompts: [],
      extensions: [],
    };

    // 1. 初始化终端 UI 宿主
    this.ui = createInteractiveTui({
      tuiMode: options.tuiMode || "regular",
      showHardwareCursor: options.showHardwareCursor ?? false,
      logDirectory: options.logDirectory || "",
    }) as TuiMainScreen;

    // 2. 初始化核心布局容器
    this.documentContainer = new Container();
    this.chatContainer = new Container();
    this.header = new HeaderComponent("0.1.0");
    this.dynamicBorder = new DynamicBorder();
    this.startupResources = new StartupResourcesComponent(options.resources);

    this.documentContainer.addChild(this.header);
    this.documentContainer.addChild(new Spacer(1));
    this.documentContainer.addChild(this.chatContainer);

    this.chatContainer.addChild(this.startupResources);

    this.pendingMessagesContainer = new Container();
    this.statusContainer = new Container();
    this.editorContainer = new Container();

    // 3. 初始化 Footer
    this.footer = new FooterComponent(
      {
        workspace: this.workspace,
        modelName: this.currentModelName,
        thinkingLevel: this.currentThinkingLevel,
        sessionName: options.sessionName,
        debugMode: Boolean(options.debug),
      },
      () => this.ui.requestRender(),
    );

    // 4. 初始化 Editor 与 Autocomplete
    this.defaultEditor = this.createEditor();
    this.editorContainer.addChild(this.defaultEditor);

    // 5. 挂载视口与组件 (对齐 Pi 原厂 mountInteractiveTui 架构规范)
    if (options.tuiMode === "fullscreen") {
      const viewport = createChatViewport({
        document: this.documentContainer,
        pendingMessages: this.pendingMessagesContainer,
        status: this.statusContainer,
        editor: this.editorContainer,
        footer: this.footer,
      });
      this.transcriptScrollView = viewport.transcript;
      if (typeof (this.ui as any).setLayoutRoot === "function") {
        (this.ui as any).setLayoutRoot(viewport.root);
      } else {
        this.ui.addChild(viewport.root);
      }
    } else {
      this.ui.addChild(this.documentContainer);
      this.ui.addChild(this.pendingMessagesContainer);
      this.ui.addChild(this.statusContainer);
      this.ui.addChild(this.editorContainer);
      this.ui.addChild(this.footer);
    }
    this.ui.setFocus(this.defaultEditor);
  }

  public async init(): Promise<void> {
    // 1. 订阅 KernelBridge 事件
    this.subscribeToBridge();

    // 2. 注册终端按键拦截
    this.setupKeybindings();

    // 3. 首次启动刷新
    this.ui.requestRender();
  }

  public start(): void {
    this.ui.start();
  }

  public stop(): void {
    this.clearStatusDisplay();
    for (const tool of this.activeToolCalls.values()) {
      tool.dispose();
    }
    this.footer.dispose();
    if (this.unsubscribeBridge) {
      this.unsubscribeBridge();
      this.unsubscribeBridge = undefined;
    }
    this.ui.stop();
  }

  public dispose(): void {
    this.stop();
  }

  // --------------------------------------------------------------------------
  // 事件处理与流式分发
  // --------------------------------------------------------------------------

  private subscribeToBridge(): void {
    this.unsubscribeBridge = this.bridge.subscribe((event: any) => {
      this.handleAgentEvent(event);
    });
  }

  public updatePendingMessagesDisplay(): void {
    this.pendingMessagesContainer.clear();
    if (this.pendingSteeringList.length === 0 && this.pendingFollowupList.length === 0) {
      this.ui.requestRender();
      return;
    }

    for (const text of this.pendingSteeringList) {
      this.pendingMessagesContainer.addChild(
        new Text(theme.dim(`Steering: ${displayMessageContent(text)}`), 0, 0),
      );
    }
    for (const text of this.pendingFollowupList) {
      this.pendingMessagesContainer.addChild(
        new Text(theme.dim(`Follow-up: ${displayMessageContent(text)}`), 0, 0),
      );
    }
    this.pendingMessagesContainer.addChild(
      new Text(theme.dim("  ↳ Alt+Q to edit all queued messages"), 0, 0),
    );
    this.ui.requestRender();
  }

  public async handleFollowUp(): Promise<void> {
    if (this.defaultEditor.disableSubmit) return;
    const text = this.defaultEditor.getText().trim();
    if (!text) return;

    const content = this.defaultEditor.getMessageContent();
    this.defaultEditor.addToHistory?.(text);
    this.defaultEditor.setText("");

    const isBusy = this.isStreaming || this.isWorking || this.isSubmitting;
    if (isBusy) {
      this.pendingFollowupList.push(typeof content === "string" ? text : content);
      this.updatePendingMessagesDisplay();
      try {
        await this.bridge.followUp(typeof content === "string" ? text : content);
      } catch (err: any) {
        this.appendErrorMessage(`Follow-up 注入异常: ${err.message || String(err)}`);
      }
    } else {
      await this.handleUserInput(text, typeof content === "string" ? undefined : content);
    }
  }

  public async restoreQueuedMessagesToEditor(_options: { abort?: boolean } = {}): Promise<void> {
    const allPending = [...this.pendingSteeringList, ...this.pendingFollowupList];
    if (allPending.length === 0) return;

    this.pendingSteeringList = [];
    this.pendingFollowupList = [];
    this.updatePendingMessagesDisplay();

    const current = this.defaultEditor.getMessageContent();
    const inputs = current.length > 0 ? [current, ...allPending] : allPending;
    const blocks: MessageContentBlock[] = [];
    inputs.forEach((content, index) => {
      if (index > 0) blocks.push({ type: "text", text: "\n\n" });
      if (typeof content === "string") blocks.push({ type: "text", text: content });
      else blocks.push(...content);
    });
    this.defaultEditor.setMessageContent(blocks);

    try {
      await this.bridge.clearQueue();
    } catch {
      // 忽略内核清空队列失败异常
    }
  }

  public handleAgentEvent(event: any): void {
    dispatchAgentEvent(this, event);
  }

  public getSlashCommands(): SlashCommand[] {
    const commands: SlashCommand[] = [...BUILTIN_SLASH_COMMANDS];

    // 添加通用的 /skill 宏命令
    commands.push({
      name: "skill",
      description: "展开并执行指定的技能",
      argumentHint: "<name> [args]",
      getArgumentCompletions: (prefix: string) => {
        const clean = prefix.trim().toLowerCase();
        const skills = this.loadedResources?.skills || [];
        return skills
          .filter((s) => s.toLowerCase().startsWith(clean))
          .map((s) => ({
            value: s,
            label: s,
            description: `Skill: ${s}`,
          }));
      },
    });

    // 为每个已发现的 skill 注入 /skill:<name> 形式的专用补全
    if (this.loadedResources?.skills) {
      for (const skill of this.loadedResources.skills) {
        commands.push({
          name: `skill:${skill}`,
          description: `展开并执行技能 ${skill}`,
          argumentHint: "[args]",
        });
      }
    }

    // 为每个已发现的 prompt 模板注入 /<name> 补全
    if (this.loadedResources?.prompts) {
      for (const prompt of this.loadedResources.prompts) {
        const pName = prompt.startsWith("/") ? prompt.slice(1) : prompt;
        if (!commands.some((c) => c.name === pName)) {
          commands.push({
            name: pName,
            description: `执行提示词模板 ${pName}`,
            argumentHint: "[args]",
          });
        }
      }
    }

    return commands;
  }

  public updateResources(resources: LoadedResourcesData): void {
    this.loadedResources = resources;
    this.startupResources.updateData(resources);
    if (!this.chatContainer.children.includes(this.startupResources)) {
      this.chatContainer.children.unshift(this.startupResources);
    }
    const fdPath = findFdPath();
    const autocompleteProvider = new CombinedAutocompleteProvider(
      this.getSlashCommands(),
      this.workspace,
      fdPath,
    );
    this.defaultEditor.setAutocompleteProvider(autocompleteProvider);
    this.ui.requestRender();
  }

  public updateFooterUsage(
    usage: Record<string, unknown>,
    contextWindow?: number,
  ): void {
    if (!usage) return;
    const u = usage as Record<string, number | undefined>;
    this.footer.update({
      inputTokens: u.input ?? u.prompt_tokens,
      outputTokens: u.output ?? u.completion_tokens,
      cacheReadTokens: u.cacheRead ?? u.cache_read,
      cacheWriteTokens: u.cacheWrite ?? u.cache_write,
      cacheHitRate: u.cacheHitRate ?? u.latestCacheHitRate,
      costUsd: u.cost ?? u.cost_usd,
      totalTokens: u.total ?? u.total_tokens,
      contextTokens: u.contextTokens ?? u.total ?? u.total_tokens,
      contextWindow: contextWindow ?? this.footer.getContextWindow(),
    });
  }

  // --------------------------------------------------------------------------
  // 编辑器与输入提交
  // --------------------------------------------------------------------------

  private createEditor(): CustomEditor {
    const editorTheme: EditorTheme = {
      borderColor: (str: string) => theme.fg("borderMuted", str),
      selectList: {
        selectedPrefix: (s: string) => theme.fg("accent", s),
        selectedText: (s: string) => theme.bold(theme.fg("accent", s)),
        description: (s: string) => theme.fg("muted", s),
        scrollInfo: (s: string) => theme.dim(s),
        noMatch: (_text: string) => theme.dim("无匹配项"),
      },
    };

    const editor = new CustomEditor(this.ui, editorTheme, {
      embedWorkingStatus: true,
    });

    editor.onChange = (text: string) => {
      const isBash = text.startsWith("!");
      if (isBash) {
        editor.borderColor = (str: string) => theme.fg("warning", str);
      } else {
        this.updateEditorBorderColor();
      }
    };
    const fdPath = findFdPath();
    const autocompleteProvider = new CombinedAutocompleteProvider(
      this.getSlashCommands(),
      this.workspace,
      fdPath,
    );
    editor.setAutocompleteProvider(autocompleteProvider);

    editor.onSubmit = async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return;
      const content = editor.getMessageContent(text);
      editor.addToHistory?.(trimmed);
      editor.setText("");
      editor.clearImageAttachments?.();
      await this.handleUserInput(trimmed, typeof content === "string" ? undefined : content);
    };

    return editor;
  }

  public async handleUserInput(input: string, content?: MessageContentBlock[]): Promise<void> {
    const isBusy = this.isStreaming || this.isWorking || this.isSubmitting;

    // ── 运行期/提交中动态分流与即时转向 (Smart Steer Routing) ──
    if (isBusy) {
      if (!content && input.startsWith("/steer")) {
        const steerText = input.replace(/^\/steer\s*/i, "").trim();
        if (steerText) {
          this.pendingSteeringList.push(steerText);
          this.updatePendingMessagesDisplay();
          await this.bridge.steer(steerText);
        } else {
          this.appendErrorMessage("用法: /steer <转向指令>");
        }
        return;
      }

      if (!content && input.startsWith("/followup")) {
        const followupText = input.replace(/^\/followup\s*/i, "").trim();
        if (followupText) {
          this.pendingFollowupList.push(followupText);
          this.updatePendingMessagesDisplay();
          await this.bridge.followUp(followupText);
        } else {
          this.appendErrorMessage("用法: /followup <后续指令>");
        }
        return;
      }

      if (!content && (input === "/abort" || input.startsWith("/abort "))) {
        void this.bridge.abort();
        this.isStreaming = false;
        this.isWorking = false;
        this.isSubmitting = false;
        this.pendingSteeringList = [];
        this.pendingFollowupList = [];
        this.updatePendingMessagesDisplay();
        if (this.currentStreamingAssistant) {
          this.chatContainer.removeChild(this.currentStreamingAssistant);
          this.currentStreamingAssistant = undefined;
        }
        this.clearStatusDisplay();
        this.footer.update({ isBusy: false });
        this.appendSystemNotice("执行已中断。");
        this.ui.requestRender();
        return;
      }

      if (!content && input.startsWith("/")) {
        this.appendErrorMessage(
          "当前智能体正在执行中，请等待完成或输入 /steer 插话、按 Esc 中断后再执行其他管理命令。",
        );
        this.ui.requestRender();
        return;
      }

      // 运行期普通文本输入直接加入 Pending 转向队列并在下方待发区展示
      this.pendingSteeringList.push(content ?? input);
      this.updatePendingMessagesDisplay();
      try {
        await this.bridge.steer(content ?? input);
      } catch (err: any) {
        this.appendErrorMessage(`转向注入异常: ${err.message || String(err)}`);
      }
      return;
    }

    // 1. 处理技能或提示词模板宏扩展
    if (
      !content &&
      input.startsWith("/") &&
      (input.startsWith("/skill:") ||
        input.startsWith("/skill ") ||
        input === "/skill" ||
        !BUILTIN_SLASH_COMMANDS.some((c) =>
          input.toLowerCase().startsWith(`/${c.name}`),
        ))
    ) {
      try {
        const client = (this.bridge as any).client;
        const macroRes =
          (await client?.request?.("macro_expand", { text: input })) ||
          (await client?.sendRequest?.("macro_expand", { text: input }));
        if (macroRes?.expanded && macroRes.text) {
          input = macroRes.text;
        } else if (
          input === "/skill" ||
          input.startsWith("/skill ") ||
          input.startsWith("/skill:")
        ) {
          const available = this.loadedResources?.skills?.length
            ? `可用技能: ${this.loadedResources.skills.join(", ")}`
            : "当前暂无可用技能。";
          this.appendErrorMessage(
            `用法: /skill <name> [args] 或 /skill:<name> [args]。${available}`,
          );
          return;
        } else if (
          !BUILTIN_SLASH_COMMANDS.some((c) =>
            input.toLowerCase().startsWith(`/${c.name}`),
          )
        ) {
          this.appendErrorMessage(
            `命令 ${input.split(" ")[0]} 暂未在当前内核模式下启用，输入 /help 查看所有可用命令。`,
          );
          return;
        }
      } catch {
        // ignore
      }
    }

    // 2. 处理斜杠命令
    if (!content && input.startsWith("/")) {
      await this.handleSlashCommand(input);
      return;
    }

    // 3. 处理 Shell 快捷命令 !cmd 或 !!cmd
    if (!content && input.startsWith("!")) {
      await this.handleShellMacro(input);
      return;
    }

    // 4. 普通文本输入：渲染用户气泡并提交给 Python
    this.isSubmitting = true;
    this.pendingSubmittedPrompts++;
    const userMsg = new UserMessageComponent(input);
    this.chatContainer.addChild(userMsg);
    this.chatContainer.addChild(new Spacer(1));
    this.ui.requestRender();

    try {
      this.footer.update({ isBusy: true });
      await this.bridge.prompt(input, content ? { content } : undefined);
    } catch (err: any) {
      this.pendingSubmittedPrompts = Math.max(0, this.pendingSubmittedPrompts - 1);
      this.appendErrorMessage(`请求失败: ${err.message || String(err)}`);
      this.isStreaming = false;
      this.isWorking = false;
      this.clearStatusDisplay();
      this.footer.update({ isBusy: false });
    } finally {
      this.isSubmitting = false;
    }
  }

  public async handleExit(): Promise<void> {
    try {
      if (this.onExit) {
        await this.onExit();
      }
    } catch {
      // 忽略退出清理异常，确保正常终止进程
    } finally {
      this.stop();
      process.exit(0);
    }
  }

  // --------------------------------------------------------------------------
  // 快捷键拦截与生命周期
  // --------------------------------------------------------------------------

  private setupKeybindings(): void {
    this.ui.addInputListener((data: string) => {
      // 若当前挂载了活动的 Selector，直接委托给 Selector 处理键盘事件并消费
      if (this.activeSelectorComponent) {
        this.activeSelectorComponent.handleInput?.(data);
        this.ui.requestRender();
        return { consume: true };
      }

      if (matchesKey(data, "ctrl+v") || matchesKey(data, "ctrl+shift+v")) {
        void this.defaultEditor.pasteImageFromClipboard().then((pasted) => {
          if (!pasted) {
            this.defaultEditor.handleInput?.(data);
          }
          this.ui.requestRender();
        });
        return { consume: true };
      } else if (matchesKey(data, "ctrl+c")) {
        if (this.isStreaming || this.isSubmitting || this.isWorking) {
          void this.bridge.abort();
          this.isStreaming = false;
          this.isWorking = false;
          this.isSubmitting = false;
          this.pendingSteeringList = [];
          this.pendingFollowupList = [];
          this.updatePendingMessagesDisplay();
          if (this.currentStreamingAssistant) {
            this.chatContainer.removeChild(this.currentStreamingAssistant);
            this.currentStreamingAssistant = undefined;
          }
          this.clearStatusDisplay();
          this.footer.update({ isBusy: false });
          this.appendSystemNotice("执行已中断。");
          this.ui.requestRender();
          return { consume: true };
        }
        if (this.defaultEditor.getText().length > 0) {
          this.defaultEditor.setText("");
          this.defaultEditor.clearImageAttachments?.();
          this.ui.requestRender();
          return { consume: true };
        }
        void this.handleExit();
        return { consume: true };
      } else if (matchesKey(data, "ctrl+d")) {
        if (this.defaultEditor.getText().length === 0 && !this.isStreaming && !this.isSubmitting) {
          void this.handleExit();
          return { consume: true };
        }
      } else if (matchesKey(data, "ctrl+q")) {
        void this.handleFollowUp();
        return { consume: true };
      } else if (matchesKey(data, "escape")) {
        if (this.isStreaming || this.isSubmitting || this.isWorking) {
          void this.bridge.abort();
          this.isStreaming = false;
          this.isWorking = false;
          this.isSubmitting = false;
          void this.restoreQueuedMessagesToEditor({ abort: true });
          if (this.currentStreamingAssistant) {
            this.chatContainer.removeChild(this.currentStreamingAssistant);
            this.currentStreamingAssistant = undefined;
          }
          this.clearStatusDisplay();
          this.footer.update({ isBusy: false });
          this.appendSystemNotice("执行已中断。");
          this.ui.requestRender();
          return { consume: true };
        }
      } else if (matchesKey(data, "alt+up") || matchesKey(data, "alt+q")) {
        if (this.pendingSteeringList.length > 0 || this.pendingFollowupList.length > 0) {
          void this.restoreQueuedMessagesToEditor();
          this.ui.requestRender();
          return { consume: true };
        }
      } else if (matchesKey(data, "ctrl+o")) {
        if (this.startupResources) {
          this.startupResources.toggleExpanded();
        }
        const targetAssistant =
          this.currentStreamingAssistant || this.latestAssistantMessage;
        if (targetAssistant) {
          targetAssistant.toggleThinking();
        }
        for (const tool of this.activeToolCalls.values()) {
          tool.toggleExpanded();
        }
        for (const child of this.chatContainer.children) {
          if (child && typeof (child as any).toggleExpanded === "function") {
            (child as any).toggleExpanded();
          }
        }
        this.ui.requestRender();
        return { consume: true };
      } else if (matchesKey(data, "ctrl+l")) {
        this.showModelSelector();
        return { consume: true };
      } else if (
        matchesKey(data, "shift+tab") ||
        data === "\x1b[Z" ||
        matchesKey(data, "ctrl+t")
      ) {
        void this.cycleThinkingLevel();
        return { consume: true };
      }
      return undefined;
    });
  }

  // --------------------------------------------------------------------------
  // 动态选择器与视口抽换 (showSelector)
  // --------------------------------------------------------------------------

  public showSelector(
    create: (done: () => void) => {
      component: Component;
      focus: Component;
      dispose?: () => void;
    },
  ): void {
    const token = {};
    let dispose: (() => void) | undefined;

    const done = () => {
      dispose?.();
      if (this.activeSelectorToken !== token) return;
      this.activeSelectorToken = undefined;
      this.activeSelectorDispose = undefined;
      this.activeSelectorComponent = undefined;
      this.editorContainer.clear();
      this.editorContainer.addChild(this.defaultEditor);
      this.ui.setFocus(this.defaultEditor);
      this.ui.requestRender();
    };

    const created = create(done);
    dispose = created.dispose;

    this.disposeActiveSelector();
    this.activeSelectorToken = token;
    this.activeSelectorDispose = dispose;
    this.activeSelectorComponent = created.component;

    this.editorContainer.clear();
    this.editorContainer.addChild(created.component);
    this.ui.setFocus(created.focus);
    this.ui.requestRender();
  }

  private disposeActiveSelector(): void {
    if (this.activeSelectorDispose) {
      this.activeSelectorDispose();
      this.activeSelectorDispose = undefined;
      this.activeSelectorToken = undefined;
      this.activeSelectorComponent = undefined;
    }
  }

  // --------------------------------------------------------------------------
  // 常用选择器封装
  // --------------------------------------------------------------------------

  public showModelSelector(initialSearch?: string): void {
    openModelSelector(this, initialSearch);
  }

  public showSessionSelector(): void {
    openSessionSelector(this);
  }

  public getSupportedThinkingLevels(): string[] {
    const m = (this.currentModelName || "").toLowerCase();
    // 1. 完全不支持思考的模型（如 gpt-4o, gpt-3.5, claude-haiku, claude-opus, deepseek-chat 等）
    if (
      m.includes("gpt-4o") ||
      m.includes("gpt-4.1") ||
      m.includes("gpt-3.5") ||
      m.includes("claude-3-5-haiku") ||
      m.includes("claude-3-opus") ||
      m === "deepseek-chat" ||
      m === "deepseek-v3"
    ) {
      return ["off"];
    }

    // 2. OpenAI o1/o3/o4 系列：API 仅支持 low, medium, high
    if (m.includes("o1") || m.includes("o3") || m.includes("o4")) {
      return ["low", "medium", "high"];
    }

    // 3. DeepSeek R1 / Reasoner：支持开启或关闭
    if (m.includes("reasoner") || m.includes("r1")) {
      return ["low", "medium", "high"];
    }

    // 4. Google Gemini 思考模型（对标 pi-antigravity，支持 off/minimal/low/medium/high，无 xhigh/max）
    if (m.includes("gemini")) {
      return ["off", "minimal", "low", "medium", "high"];
    }

    // 5. Claude 3.7 Sonnet 系列：支持连续 Token 预算，完整映射至 max
    if (
      m.includes("claude") &&
      (m.includes("sonnet") || m.includes("3-7") || m.includes("4"))
    ) {
      return ["off", "minimal", "low", "medium", "high", "max"];
    }

    return ["off", "low", "medium", "high"];
  }

  public updateEditorBorderColor(): void {
    if (this.defaultEditor) {
      if (this.defaultEditor.getText().startsWith("!")) {
        this.defaultEditor.borderColor = (str: string) =>
          theme.fg("warning", str);
      } else {
        this.defaultEditor.borderColor = theme.getThinkingBorderColor(
          this.currentThinkingLevel,
        );
      }
      this.ui.requestRender();
    }
  }

  public async cycleThinkingLevel(): Promise<string | undefined> {
    const levels = this.getSupportedThinkingLevels();
    if (levels.length === 1 && levels[0] === "off") {
      this.currentThinkingLevel = "off";
      this.footer.update({ thinkingLevel: "off" });
      this.updateEditorBorderColor();
      this.appendSystemNotice("当前模型不支持思考模式 (thinking: off)");
      this.ui.requestRender();
      return undefined;
    }

    const curIdx = levels.indexOf(this.currentThinkingLevel.toLowerCase());
    const nextIdx = (curIdx + 1) % levels.length;
    const nextLevel = levels[nextIdx];

    this.currentThinkingLevel = nextLevel;
    this.footer.update({ thinkingLevel: nextLevel });
    this.updateEditorBorderColor();
    this.appendSystemNotice(`✓ 思考预算已更新为: ${nextLevel}`);
    this.ui.requestRender();

    try {
      await this.bridge.setThinking(nextLevel);
    } catch {
      // ignore
    }
    return nextLevel;
  }

  public showThinkingSelector(): void {
    openThinkingSelector(this);
  }

  public showLoginSelector(): void {
    openLoginSelector(this);
  }

  public showLogoutSelector(): void {
    openLogoutSelector(this);
  }

  public showThemeSelector(): void {
    openThemeSelector(this);
  }

  public showTreeSelector(): void {
    openTreeSelector(this);
  }

  public async showForkSelector(): Promise<void> {
    await openForkSelector(this);
  }

  public async showSettingsSelector(): Promise<void> {
    await openSettingsSelector(this);
  }

  // --------------------------------------------------------------------------
  // 斜杠命令分发
  // --------------------------------------------------------------------------

  public async handleSlashCommand(input: string): Promise<void> {
    await executeSlashCommand(this, input);
  }

  private async handleShellMacro(input: string): Promise<void> {
    const isSilent = input.startsWith("!!");
    const rawCmd = input.replace(/^!!?/, "").trim();
    if (!rawCmd) {
      this.appendErrorMessage(
        "请输入要执行的本地 Shell 命令，例如：!git status 或 !!ls -la",
      );
      return;
    }
    try {
      const client = (this.bridge as any).client;
      let output = "";
      let exitCode = 0;
      if (client?.sendRequest) {
        const res = await client.sendRequest("shell_exec", {
          command: rawCmd,
          exclude_from_context: isSilent,
        });
        if (res?.output !== undefined) output = res.output;
        if (res?.exit_code !== undefined) exitCode = res.exit_code;
      }
      this.appendSystemNotice(
        `$ ${rawCmd} (${isSilent ? "静默执行，未加入上下文" : "已加入上下文"}) (Exit: ${exitCode})\n\n\`\`\`text\n${output}\n\`\`\``,
      );
    } catch (err: any) {
      this.appendErrorMessage(`执行失败: ${err.message || String(err)}`);
    }
  }

  public renderSessionHistory(messages: any[], banner?: string): void {
    renderSessionHistory(this, messages, banner);
  }

  public renderPiSessionStats(stats: any): void {
    renderPiSessionStats(this, stats);
  }

  public renderFallbackSessionStats(): void {
    renderFallbackSessionStats(this);
  }

  // --------------------------------------------------------------------------
  // 辅助渲染方法
  // --------------------------------------------------------------------------

  public appendSystemNotice(text: string): void {
    const notice = new Text(theme.fg("accent", text), 1, 0);
    this.chatContainer.addChild(notice);
    this.ui.requestRender();
  }

  public appendErrorMessage(text: unknown): void {
    const str =
      typeof text === "string"
        ? text
        : Array.isArray(text)
          ? text
              .map((b: any) =>
                typeof b === "string" ? b : b?.text || b?.thinking || "",
              )
              .join("")
          : typeof text === "object" && text !== null
            ? (text as any).message || JSON.stringify(text)
            : String(text ?? "");
    const errorNotice = new Text(theme.fg("error", `⚠ ${str}`), 1, 0);
    this.chatContainer.addChild(errorNotice);
    this.ui.requestRender();
  }

  public activeStatusIndicator: StatusIndicator | null = null;

  public showWorkingStatusIndicator(message = "Working"): void {
    this.clearStatusDisplay();
    const colorFn = (str: string) =>
      theme.getThinkingBorderColor(this.currentThinkingLevel)(str);
    const indicator = new WorkingStatusIndicator(
      this.ui,
      message,
      undefined,
      colorFn,
    );
    indicator.start();
    this.activeStatusIndicator = indicator;
    this.defaultEditor.setWorkingStatusIndicator(indicator);
    this.footer.update({ isBusy: true });
    this.ui.requestRender();
  }

  private updateStatusDisplay(text: string = "Working"): void {
    this.showWorkingStatusIndicator(text);
  }

  public clearStatusDisplay(): void {
    if (this.activeStatusIndicator) {
      this.activeStatusIndicator.dispose();
      this.activeStatusIndicator = null;
    }
    this.defaultEditor.setWorkingStatusIndicator(undefined);
    this.statusContainer.clear();
    this.ui.requestRender();
  }
}
