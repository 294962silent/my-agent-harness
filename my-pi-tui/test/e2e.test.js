import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { PythonKernelClient } from "../dist/client.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "../..");

// 该用例会真实拉起 Python 内核，initialize 阶段需要解析模型凭据。
// CI 上没有 auth.json / API key，跳过而不是伪造凭据。
const hasCredential =
  Boolean(
    process.env.OPENAI_API_KEY ||
      process.env.DEEPSEEK_API_KEY ||
      process.env.ANTHROPIC_API_KEY,
  ) || fs.existsSync(path.join(os.homedir(), ".my-pi-agent", "auth.json"));

const skipReason = hasCredential
  ? false
  : "无模型凭据（auth.json / *_API_KEY），跳过真实内核冒烟";

test("End-to-End: PythonKernelClient launches real Python rpc_server and manages lifecycle requests", { skip: skipReason }, async (t) => {
  const client = new PythonKernelClient({
    workspace: repoRoot,
  });

  t.after(async () => {
    await client.shutdown();
  });

  // 1. Start client & spawn real Python rpc_server
  await client.start();
  assert.ok(client.child !== null, "Child process must be spawned");
  assert.ok(
    typeof client.child.pid === "number" && client.child.pid > 0,
    "Child PID must be valid",
  );
  assert.equal(client.child.killed, false, "Child process should be running");

  // 2. Send steer
  await client.steer("test steering message");

  // 3. Send followup
  await client.followup("test followup task");

  // 4. Send abort
  await client.abort();

  assert.ok(
    true,
    "Client successfully communicated with Python kernel over stdio JSON-RPC",
  );
});
