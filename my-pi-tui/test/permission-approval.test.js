import test from "node:test";
import assert from "node:assert/strict";

import { PythonKernelClient } from "../dist/client.js";
import { KernelBridge } from "../dist/bridge/kernel-bridge.js";
import {
  buildPreviewLines,
  PermissionDialogComponent,
  permissionOptionToResult,
  truncateSingleLine,
} from "../dist/components/permission-dialog.js";
import { InteractiveMode } from "../dist/interactive/interactive-mode.js";

function makeRequest(overrides = {}) {
  return {
    request_id: "perm-1",
    tool_call_id: "call_abc123",
    tool_name: "bash",
    action: "bash",
    target: "rm -rf build",
    preview: null,
    risk: "high",
    reason: "删除工作区内的构建目录",
    details: { command: "rm -rf build" },
    timeout_ms: 300000,
    ...overrides,
  };
}

function makeMockChild() {
  const writes = [];
  return {
    writes,
    child: {
      stdin: {
        write(data) {
          writes.push(data);
        },
      },
      stdout: null,
      kill() {},
      on() {},
    },
  };
}

// ---------------------------------------------------------------------------
// client.ts
// ---------------------------------------------------------------------------

test("PythonKernelClient emits permission_request instead of treating it as a response", () => {
  const client = new PythonKernelClient();
  const { child } = makeMockChild();
  client.child = child;

  const received = [];
  client.on("permission_request", (params) => received.push(params));

  const params = makeRequest();
  client.handleLine(
    JSON.stringify({
      jsonrpc: "2.0",
      id: "perm-1",
      method: "permission_request",
      params,
    }),
  );

  assert.equal(received.length, 1);
  assert.deepEqual(received[0], params);

  // A server request carrying an id must not settle a pending client request.
  let settled = false;
  const pending = client.sendRequest("prompt", { text: "hi" });
  pending.then(() => {
    settled = true;
  });
  client.handleLine(
    JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "permission_request",
      params: makeRequest({ request_id: "perm-2" }),
    }),
  );
  assert.equal(received.length, 2);
  assert.equal(settled, false);
});

test("respondPermission writes a JSON-RPC response with the server request id", async () => {
  const client = new PythonKernelClient();
  const { child, writes } = makeMockChild();
  client.child = child;

  client.respondPermission("perm-1", { approved: true, scope: "session" });

  assert.equal(writes.length, 1);
  assert.ok(writes[0].endsWith("\n"));
  assert.deepEqual(JSON.parse(writes[0]), {
    jsonrpc: "2.0",
    id: "perm-1",
    result: { approved: true, scope: "session" },
  });

  client.respondPermission("perm-1", { approved: false });
  assert.deepEqual(JSON.parse(writes[1]), {
    jsonrpc: "2.0",
    id: "perm-1",
    result: { approved: false },
  });
});

test("respondPermission is a safe no-op when the kernel is not running", () => {
  const client = new PythonKernelClient();
  assert.doesNotThrow(() => {
    client.respondPermission("perm-missing", { approved: false });
  });
});

// ---------------------------------------------------------------------------
// permission-dialog.ts
// ---------------------------------------------------------------------------

test("PermissionDialogComponent renders title, risk, target, preview, reason and options", () => {
  const preview = Array.from({ length: 15 }, (_, i) => `preview line ${i + 1}`).join("\n");
  const dialog = new PermissionDialogComponent(
    makeRequest({ preview }),
    () => {},
  );

  const text = dialog.render(100).join("\n");

  assert.ok(text.includes("需要授权 · bash"));
  assert.ok(text.includes("[high]"));
  assert.ok(text.includes("目标:"));
  assert.ok(text.includes("rm -rf build"));
  assert.ok(text.includes("预览:"));
  assert.ok(text.includes("preview line 1"));
  assert.ok(text.includes("preview line 12"));
  assert.ok(!text.includes("preview line 13"));
  assert.ok(text.includes("…"));
  assert.ok(text.includes("原因:"));
  assert.ok(text.includes("删除工作区内的构建目录"));
  assert.ok(text.includes("允许这一次"));
  assert.ok(text.includes("本会话都允许"));
  assert.ok(text.includes("拒绝"));
  assert.ok(text.includes("↑↓ 选择"));
  assert.ok(text.includes("Enter 确认"));
  assert.ok(text.includes("Esc 拒绝"));
});

test("PermissionDialogComponent renders risk badge colors as dim/warning/error", () => {
  const cases = [
    ["high", "[high]", "error"],
    ["medium", "[medium]", "warning"],
    ["low", "[low]", "dim"],
  ];

  for (const [risk, badge] of cases) {
    const dialog = new PermissionDialogComponent(
      makeRequest({ risk, preview: null }),
      () => {},
    );
    const text = dialog.render(80).join("\n");
    assert.ok(text.includes(badge), `${risk} badge should be rendered`);
  }
});

test("PermissionDialogComponent truncates long target to one line with ellipsis", () => {
  const longTarget = `bash command ${"x".repeat(200)}`;
  const dialog = new PermissionDialogComponent(
    makeRequest({ target: longTarget, preview: null }),
    () => {},
  );

  const wideText = dialog.render(160).join("\n");
  assert.ok(wideText.includes("…"));
  assert.ok(!wideText.includes(longTarget));

  // Even at a narrow width the target must remain a single rendered line.
  const targetLines = dialog
    .render(60)
    .filter((line) => line.includes("bash command xxx"));
  assert.equal(targetLines.length, 1);
  assert.ok(targetLines[0].includes("…"));

  assert.equal(truncateSingleLine("line1\nline2", 40), "line1 line2");
});

test("buildPreviewLines caps preview at 12 lines and appends an ellipsis", () => {
  const lines = buildPreviewLines(
    Array.from({ length: 30 }, (_, i) => `line-${i + 1}`).join("\n"),
  );
  assert.equal(lines.length, 13);
  assert.equal(lines[11], "line-12");
  assert.equal(lines[12], "…");

  const short = buildPreviewLines("a\nb\n");
  assert.deepEqual(short, ["a", "b"]);
  assert.deepEqual(buildPreviewLines(null), []);
});

test("PermissionDialogComponent maps Enter on each option to the right result", () => {
  const cases = [
    { keys: [], expected: { approved: true, scope: "once" } },
    { keys: ["\x1b[B"], expected: { approved: true, scope: "session" } },
    {
      keys: ["\x1b[B", "\x1b[B"],
      expected: { approved: false },
    },
  ];

  for (const { keys, expected } of cases) {
    const decisions = [];
    const dialog = new PermissionDialogComponent(makeRequest(), (result) => {
      decisions.push(result);
    });
    for (const key of keys) dialog.handleInput(key);
    dialog.handleInput("\r");

    assert.equal(decisions.length, 1);
    assert.deepEqual(decisions[0], expected);
  }
});

test("PermissionDialogComponent maps Escape (and Ctrl+C) to deny", () => {
  for (const key of ["\x1b", "\x03"]) {
    const decisions = [];
    const dialog = new PermissionDialogComponent(makeRequest(), (result) => {
      decisions.push(result);
    });
    dialog.handleInput(key);
    assert.deepEqual(decisions, [{ approved: false }]);
    assert.equal(dialog.isSettled, true);
  }
});

test("PermissionDialogComponent fails closed when child rendering throws", () => {
  const decisions = [];
  const dialog = new PermissionDialogComponent(makeRequest(), (result) => {
    decisions.push(result);
  });
  dialog.addChild({
    invalidate() {},
    render() {
      throw new Error("render exploded");
    },
  });

  const lines = dialog.render(80);
  assert.deepEqual(decisions, [{ approved: false }]);
  assert.ok(lines.join("\n").includes("审批弹窗渲染失败"));
});

test("PermissionDialogComponent ignores input after a decision", () => {
  const decisions = [];
  const dialog = new PermissionDialogComponent(makeRequest(), (result) => {
    decisions.push(result);
  });

  dialog.handleInput("\r");
  dialog.handleInput("\x1b");
  dialog.handleInput("\x1b[B");
  dialog.handleInput("\r");

  assert.deepEqual(decisions, [{ approved: true, scope: "once" }]);
});

test("permissionOptionToResult maps all three options and defaults to once", () => {
  assert.deepEqual(permissionOptionToResult("once"), { approved: true, scope: "once" });
  assert.deepEqual(permissionOptionToResult("session"), {
    approved: true,
    scope: "session",
  });
  assert.deepEqual(permissionOptionToResult("deny"), { approved: false });
  assert.deepEqual(permissionOptionToResult("unknown"), {
    approved: true,
    scope: "once",
  });
});

// ---------------------------------------------------------------------------
// interactive-mode.ts integration
// ---------------------------------------------------------------------------

function createPermissionBridge({ hasChild = true } = {}) {
  const listeners = new Map();
  const responses = [];
  const writes = [];

  const client = {
    child: hasChild
      ? {
          stdin: {
            write(data) {
              writes.push(data);
            },
          },
        }
      : null,
    on(event, listener) {
      if (!listeners.has(event)) listeners.set(event, []);
      listeners.get(event).push(listener);
    },
    off(event, listener) {
      const bucket = listeners.get(event) || [];
      listeners.set(
        event,
        bucket.filter((entry) => entry !== listener),
      );
    },
    request: async () => ({ status: "ok" }),
    sendRequest: async () => ({ status: "ok" }),
    respondPermission(id, result) {
      responses.push({ id, result });
    },
  };

  return {
    client,
    responses,
    writes,
    emit(params) {
      for (const listener of listeners.get("permission_request") || []) {
        listener(params);
      }
    },
  };
}

test("InteractiveMode shows the permission dialog and responds with the selected option", async () => {
  const { client, responses, emit } = createPermissionBridge();
  const mode = new InteractiveMode(new KernelBridge(client));
  await mode.init();

  const request = makeRequest();
  emit(request);

  assert.ok(mode.activeSelectorComponent);
  const rendered = mode.ui.render(100).join("\n");
  assert.ok(rendered.includes("需要授权 · bash"));
  assert.ok(rendered.includes("rm -rf build"));

  // ↓ then Enter -> session scope.
  mode.activeSelectorComponent.handleInput("\x1b[B");
  mode.activeSelectorComponent.handleInput("\r");

  assert.deepEqual(responses, [
    { id: "perm-1", result: { approved: true, scope: "session" } },
  ]);
  assert.equal(mode.activeSelectorComponent, undefined);
  const after = mode.ui.render(100).join("\n");
  assert.ok(!after.includes("需要授权 · bash"));
});

test("InteractiveMode maps Esc in the permission dialog to a deny response", async () => {
  const { client, responses, emit } = createPermissionBridge();
  const mode = new InteractiveMode(new KernelBridge(client));
  await mode.init();

  emit(makeRequest({ request_id: "perm-esc" }));
  assert.ok(mode.activeSelectorComponent);

  mode.activeSelectorComponent.handleInput("\x1b");

  assert.deepEqual(responses, [
    { id: "perm-esc", result: { approved: false } },
  ]);
  assert.equal(mode.activeSelectorComponent, undefined);
});

test("InteractiveMode denies immediately when the kernel is not running", async () => {
  const { client, responses, emit } = createPermissionBridge({ hasChild: false });
  const mode = new InteractiveMode(new KernelBridge(client));
  await mode.init();

  emit(makeRequest({ request_id: "perm-dead" }));

  assert.deepEqual(responses, [
    { id: "perm-dead", result: { approved: false } },
  ]);
  assert.equal(mode.activeSelectorComponent, undefined);
});

test("InteractiveMode denies when mounting the permission dialog throws", async () => {
  const { client, responses, emit } = createPermissionBridge();
  const mode = new InteractiveMode(new KernelBridge(client));
  await mode.init();

  const broken = makeRequest({ request_id: "perm-broken" });
  Object.defineProperty(broken, "preview", {
    enumerable: true,
    get() {
      throw new Error("broken preview getter");
    },
  });

  emit(broken);

  assert.deepEqual(responses, [
    { id: "perm-broken", result: { approved: false } },
  ]);
  assert.equal(mode.activeSelectorComponent, undefined);
});

test("InteractiveMode queues permission requests and shows them one at a time", async () => {
  const { client, responses, emit } = createPermissionBridge();
  const mode = new InteractiveMode(new KernelBridge(client));
  await mode.init();

  emit(makeRequest({ request_id: "perm-a", target: "first-target" }));
  emit(makeRequest({ request_id: "perm-b", target: "second-target" }));

  assert.ok(mode.activeSelectorComponent);
  assert.ok(mode.ui.render(100).join("\n").includes("first-target"));

  mode.activeSelectorComponent.handleInput("\r"); // allow once
  assert.deepEqual(responses, [
    { id: "perm-a", result: { approved: true, scope: "once" } },
  ]);

  // Second request is shown after the first dialog is dismissed.
  assert.ok(mode.activeSelectorComponent);
  assert.ok(mode.ui.render(100).join("\n").includes("second-target"));

  mode.activeSelectorComponent.handleInput("\x1b[B");
  mode.activeSelectorComponent.handleInput("\x1b[B");
  mode.activeSelectorComponent.handleInput("\r"); // deny

  assert.deepEqual(responses, [
    { id: "perm-a", result: { approved: true, scope: "once" } },
    { id: "perm-b", result: { approved: false } },
  ]);
  assert.equal(mode.activeSelectorComponent, undefined);
});
