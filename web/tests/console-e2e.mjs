/** Real browser + real WTB API/CAS acceptance. No network mocks or synthetic runs.
 * Start an isolated console and Vite first, then:
 * WTB_E2E_URL=http://127.0.0.1:5173 npm run test:e2e
 */
import assert from "node:assert/strict";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

const url = process.env.WTB_E2E_URL || "http://127.0.0.1:5173";
const output = path.resolve(
  process.env.WTB_E2E_OUTPUT || "../output/playwright/e2e",
);
await mkdir(output, { recursive: true });
const browser = await chromium.launch({
  headless: true,
  ...(process.env.WTB_E2E_CHANNEL
    ? { channel: process.env.WTB_E2E_CHANNEL }
    : {}),
});
const context = await browser.newContext({
  viewport: { width: 1440, height: 1000 },
  permissions: ["clipboard-read", "clipboard-write"],
});
const page = await context.newPage();
page.setDefaultTimeout(10000);
const errors = [],
  results = [];
page.on("pageerror", (error) => errors.push(String(error)));
const button = (name) => page.getByRole("button", { name, exact: true });
const nav = async (name) =>
  page
    .getByRole("navigation", { name: "控制台功能" })
    .getByRole("button", { name, exact: true })
    .click();
const get = async (endpoint) => {
  const response = await context.request.get(url + "/api/v1" + endpoint);
  assert(response.ok(), `${endpoint}: ${response.status()}`);
  return response.json();
};
async function until(fn, description) {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    const value = await fn();
    if (value) return value;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out: ${description}`);
}
async function check(name, action) {
  const start = Date.now();
  try {
    await action();
    results.push({ name, passed: true, ms: Date.now() - start });
    console.log("PASS", name);
  } catch (error) {
    results.push({
      name,
      passed: false,
      error: String(error),
      ms: Date.now() - start,
    });
    await page
      .screenshot({ path: path.join(output, "failure.png"), fullPage: true })
      .catch(() => {});
    throw error;
  }
}
async function settled(id, status) {
  const execution = await until(async () => {
    const e = await get(`/executions/${id}`);
    return !e.pendingOperation && e.status === status && e;
  }, `${id} becomes ${status}`);
  await until(
    async () =>
      (await page.getByTestId("execution-status").textContent()) === status,
    `UI ${status}`,
  );
  return execution;
}
async function action(name) {
  await button(name).click();
}
async function saveState(state) {
  await action("编辑状态");
  await page
    .getByRole("textbox", { name: "状态 JSON" })
    .fill(JSON.stringify(state));
  const response = page.waitForResponse(
    (r) => r.url().endsWith("/state") && r.request().method() === "POST",
  );
  await action("保存");
  assert.equal((await response).status(), 202);
  await page
    .getByRole("dialog", { name: "编辑执行状态" })
    .waitFor({ state: "hidden" });
}
async function launch(state, { breakpoints = [], override, variantId } = {}) {
  await action("新建执行");
  if (variantId)
    await page.getByLabel("启动变体", { exact: true }).selectOption(variantId);
  await page
    .getByRole("textbox", { name: "状态 JSON" })
    .fill(JSON.stringify(state));
  for (const node of breakpoints)
    await page.getByLabel(`断点 ${node}`, { exact: true }).check();
  if (override)
    await page
      .getByLabel("transform 实现", { exact: true })
      .selectOption(override);
  const response = page.waitForResponse(
    (r) => r.url().endsWith("/execute") && r.request().method() === "POST",
  );
  await action("启动执行");
  const result = await response;
  assert.equal(result.status(), 202);
  const job = await result.json();
  await page
    .getByRole("dialog", { name: "启动工作流" })
    .waitFor({ state: "hidden" });
  await page.getByTestId("execution-status").waitFor();
  return job.executionId;
}
async function selectCheckpoint(id, cp) {
  await action("检查点");
  await page
    .getByRole("button", { name: new RegExp(`步骤 .*${cp.slice(0, 8)}`) })
    .click();
  await until(
    async () => !(await button("手动检查点").isEnabled()),
    "historical checkpoint selected",
  );
}
async function restore(kind) {
  await action(kind === "fork" ? "从所选检查点 Fork" : "回退到所选检查点");
  const response = page.waitForResponse(
    (r) =>
      r.url().endsWith(kind === "fork" ? "/branches" : "/rollback") &&
      r.request().method() === "POST",
  );
  await button(kind === "fork" ? "创建分支" : "确认回退").click();
  const res = await response;
  assert.equal(res.status(), 202);
  const job = await res.json();
  const op = await until(async () => {
    const result = await get("/operations/" + job.operationId);
    if (result.status === "failed") throw Error(result.error);
    return result.status === "completed" && result;
  }, kind);
  await page.locator("#action-dialog").waitFor({ state: "hidden" });
  return op.resultExecutionId || job.executionId;
}
let root, child, initialCheckpoint, parentBytes, project;
try {
  await page.goto(url);
  const catalog = await get("/catalog");
  project = catalog.projects[0].id;
  await check(
    "empty console has all feature entries and a working launch control",
    async () => {
      assert.equal(
        (await get("/executions")).total,
        0,
        "Run against a fresh data directory",
      );
      await until(() => button("新建执行").isEnabled(), "launch enabled");
      assert.equal(
        await page
          .getByRole("navigation", { name: "控制台功能" })
          .getByRole("button")
          .count(),
        7,
      );
      assert.equal(
        await page.locator("body").getAttribute("data-view"),
        "loom",
      );
      await page.screenshot({
        path: path.join(output, "01-empty.png"),
        fullPage: true,
      });
    },
  );
  await check(
    "launch validates object JSON and modal keyboard does not switch views",
    async () => {
      await action("新建执行");
      await page.getByRole("textbox", { name: "状态 JSON" }).fill("[]");
      await action("启动执行");
      assert.match(
        await page.getByRole("dialog", { name: "启动工作流" }).innerText(),
        /JSON 对象/,
      );
      await page.getByRole("textbox", { name: "状态 JSON" }).press("1");
      assert.equal(
        await page.locator("body").getAttribute("data-view"),
        "loom",
      );
      await action("取消");
      assert.equal((await get("/executions")).total, 0);
    },
  );
  await check(
    "first real run pauses before transform without a checkpoint render crash",
    async () => {
      root = await launch(
        { text: "E2E parent", repeat: 3, delay: 0.2 },
        { breakpoints: ["transform"] },
      );
      const e = await settled(root, "paused");
      assert.deepEqual(e.nextNodes, ["transform"]);
      assert(e.nodeRuns.every((n) => n.nodeId !== "transform"));
      initialCheckpoint = e.checkpointId;
      assert.equal(
        await readFile(path.join(e.runtime.output, "report.txt"), "utf8"),
        "draft:E2E parent",
      );
      assert.equal(errors.length, 0);
    },
  );
  await check(
    "CAS files and checkpoint state are accessible from the browser",
    async () => {
      await action("文件版本");
      await until(
        async () =>
          (await page.locator(".history pre").innerText()).includes(
            "draft:E2E parent",
          ),
        "CAS preview",
      );
      await action("状态");
      assert.match(
        await page.locator(".history pre").innerText(),
        /E2E parent/,
      );
      await action("事件");
      await until(
        async () => (await page.locator(".history tbody tr").count()) > 0,
        "real audit",
      );
    },
  );
  await check(
    "manual checkpoint creates a new persisted boundary",
    async () => {
      await action("创建检查点");
      await until(
        async () =>
          (await get(`/executions/${root}`)).checkpointId !== initialCheckpoint,
        "new checkpoint",
      );
      await until(() => button("编辑状态").isEnabled(), "operation settled");
    },
  );
  await check(
    "state editing and breakpoint changes affect real execution",
    async () => {
      await saveState({ text: "E2E edited", repeat: 3 });
      await action("设置断点");
      await page.getByLabel("断点 transform", { exact: true }).uncheck();
      await action("保存");
      await page
        .getByRole("dialog", { name: "设置执行断点" })
        .waitFor({ state: "hidden" });
      await action("继续运行");
      const e = await settled(root, "completed");
      assert.equal(
        e.nodeRuns.filter(
          (n) => n.nodeId === "transform" && n.status === "completed",
        ).length,
        3,
      );
      assert.equal(
        await readFile(path.join(e.runtime.output, "report.txt"), "utf8"),
        "final:pass 3:E2E edited",
      );
    },
  );
  await check(
    "file-version comparison reads two different CAS commits",
    async () => {
      await action("文件版本");
      await page
        .getByLabel("对比检查点", { exact: true })
        .selectOption(initialCheckpoint);
      await until(
        async () =>
          (await page.locator(".history pre").allTextContents())
            .join("\n")
            .includes("draft:E2E parent"),
        "before version",
      );
      assert.match(
        (await page.locator(".history pre").allTextContents()).join("\n"),
        /final:pass 3:E2E edited/,
      );
    },
  );
  await check(
    "rollback restores graph state and real output; history is preserved",
    async () => {
      await selectCheckpoint(root, initialCheckpoint);
      await restore("rollback");
      const e = await settled(root, "paused");
      assert.equal(e.state.text, "E2E parent");
      assert.equal(e.attempts.at(-1).kind, "rollback");
      parentBytes = await readFile(
        path.join(e.runtime.output, "report.txt"),
        "utf8",
      );
      assert.equal(parentBytes, "draft:E2E parent");
      assert(
        e.nodeRuns.some((n) => n.nodeId === "finish"),
        "old history retained",
      );
    },
  );
  await check(
    "fork creates independent workspace, restores CAS, and resumes edited child",
    async () => {
      child = await restore("fork");
      let e = await settled(child, "paused");
      const parent = await get(`/executions/${root}`);
      assert.notEqual(e.runtime.workspace, parent.runtime.workspace);
      assert.equal(e.parentId, root);
      await saveState({ text: "E2E child", repeat: 2 });
      await action("继续运行");
      e = await settled(child, "completed");
      assert.equal(
        await readFile(path.join(e.runtime.output, "report.txt"), "utf8"),
        "final:pass 2:E2E child",
      );
      assert.equal(
        await readFile(path.join(parent.runtime.output, "report.txt"), "utf8"),
        parentBytes,
      );
      await action("执行分支");
      await page
        .getByRole("button", { name: "父执行 " + root.slice(0, 8) })
        .click();
      await settled(root, "paused");
      await action("执行分支");
      await page
        .getByRole("button", { name: "子执行 " + child.slice(0, 8) })
        .click();
      await settled(child, "completed");
    },
  );
  await check(
    "unrun variant stays idle, and all persisted loop checkpoints remain selectable",
    async () => {
      await nav("结构视图");
      await page
        .getByRole("button", { name: /^transform:uppercase ·/ })
        .locator("text")
        .nth(1)
        .click();
      assert.equal(
        await page
          .getByLabel("选择此变体的运行实例", { exact: true })
          .inputValue(),
        "",
      );
      assert.equal(await button("Fork").isEnabled(), false);
      await page
        .getByRole("button", { name: /^default ·/ })
        .locator("text")
        .nth(1)
        .click();
      await page
        .getByLabel("选择此变体的运行实例", { exact: true })
        .selectOption(root);
      const cps = (await get(`/executions/${root}/checkpoints?limit=200`))
        .items;
      assert.equal(
        await page
          .getByLabel("选择检查点", { exact: true })
          .locator("option")
          .count(),
        cps.length + 1,
      );
      await page.getByRole("button", { name: /02.*分支地形/ }).click();
      assert.equal(
        await page.getByRole("button", { name: /个跟踪文件/ }).count(),
        cps.length +
          (await get(`/executions/${child}/checkpoints?limit=200`)).total,
      );
      await page.screenshot({
        path: path.join(output, "02-lineage.png"),
        fullPage: true,
      });
    },
  );
  await check("context drawer, clipboard, guide and export work", async () => {
    await action("执行上下文");
    const drawer = page.getByRole("dialog", { name: "执行上下文" });
    await drawer
      .getByRole("button", { name: "选择节点 prepare", exact: true })
      .click();
    assert.match(await drawer.innerText(), /Local worker thread/);
    await drawer
      .getByRole("button", { name: "复制 本地根目录", exact: true })
      .click();
    assert.equal(
      await page.evaluate(() => navigator.clipboard.readText()),
      (await get(`/executions/${root}`)).runtime.workspace,
    );
    await action("关闭执行上下文");
    await page.locator("#guide-btn").click();
    await page.locator("#guide-dialog").waitFor({ state: "visible" });
    await page.keyboard.press("Escape");
    const download = page.waitForEvent("download");
    await action("导出状态");
    const exported = await download;
    await exported.saveAs(path.join(output, "projection.json"));
    const projection = JSON.parse(
      await readFile(path.join(output, "projection.json"), "utf8"),
    );
    assert(projection.runs.some((r) => r.id === root));
    await action("刷新");
  });
  await check(
    "failed execution is recoverable by rollback, state edit and resume",
    async () => {
      const id = await launch({ text: "recover", fail: true, delay: 0.1 });
      let e = await settled(id, "failed");
      assert.match(
        await page.getByRole("alert").first().innerText(),
        /Requested failure/,
      );
      assert.equal(await button("继续运行").isEnabled(), false);
      const cp = (await get(`/executions/${id}/checkpoints`)).items.find(
        (c) => c.lastNode === "prepare",
      );
      await selectCheckpoint(id, cp.checkpointId);
      await restore("rollback");
      await settled(id, "paused");
      await saveState({ fail: false });
      await action("继续运行");
      e = await settled(id, "completed");
      assert.equal(e.state.fail, false);
    },
  );
  await check(
    "pause waits for a node boundary and stop reaches cancelled",
    async () => {
      const id = await launch({ text: "pause-stop", repeat: 8, delay: 1 });
      await action("暂停运行");
      await settled(id, "paused");
      await action("停止执行");
      await settled(id, "cancelled");
    },
  );
  await check(
    "launch implementation override changes the real output",
    async () => {
      const id = await launch(
        { text: "mixedCase", delay: 0.1 },
        { override: "uppercase" },
      );
      const e = await settled(id, "completed");
      assert.equal(
        await readFile(path.join(e.runtime.output, "report.txt"), "utf8"),
        "final:PASS 1:MIXEDCASE",
      );
    },
  );
  await check(
    "batch validates inputs and isolates successful and failed real runs",
    async () => {
      await nav("批量实验");
      await page
        .getByRole("checkbox", {
          name: `${project}/transform:uppercase`,
          exact: true,
        })
        .check();
      await page.getByRole("textbox", { name: "批量输入" }).fill("{}");
      await action("提交批量实验");
      assert.match(
        await page.locator(".panel-body").innerText(),
        /JSON 对象数组/,
      );
      await page.getByRole("textbox", { name: "批量输入" }).fill(
        JSON.stringify([
          { text: "batch", delay: 0.1 },
          { text: "bad", fail: true, delay: 0.1 },
        ]),
      );
      const response = page.waitForResponse(
        (r) =>
          r.url().endsWith("/batch-tests") && r.request().method() === "POST",
      );
      await action("提交批量实验");
      const batch = await (await response).json();
      await until(async () => {
        const { items } = await get(`/batch-tests/${batch.id}/results`);
        return items.every((e) => ["completed", "failed"].includes(e.status));
      }, "batch terminal");
      await until(
        async () =>
          (await page.locator(".panel-body").innerText()).includes(
            "完成 2 / 2",
          ),
        "batch UI",
      );
      assert.match(await page.locator(".panel-body").innerText(), /50%/);
      await button("查看 / 回退 / Fork").first().click();
      await page.getByTestId("execution-status").waitFor();
    },
  );
  await check(
    "component library, comparison, audit pagination and CAS integrity work",
    async () => {
      await nav("组件库");
      assert((await page.locator("tbody tr").count()) >= 3);
      await nav("变体对照");
      await page
        .getByLabel("对照变体 1", { exact: true })
        .selectOption(`${project}/transform:uppercase`);
      assert.equal(await page.locator(".react-flow").count(), 2);
      await nav("审计");
      await until(() => button("下一页").isEnabled(), "audit page loaded");
      await action("下一页");
      await until(() => button("上一页").isEnabled(), "audit next");
      await action("上一页");
      await nav("系统");
      await action("检查文件完整性");
      await until(
        async () =>
          (await page.locator(".panel-body").innerText()).includes(
            "检查已完成",
          ),
        "integrity completed",
      );
      const system = await get("/system");
      assert.equal(
        system.integrity[0].status,
        "healthy",
        JSON.stringify(system.integrity),
      );
    },
  );
  await check(
    "four projections and responsive 1440/900/390 layouts stay within viewport",
    async () => {
      await nav("结构视图");
      for (const width of [1440, 900, 390]) {
        await page.setViewportSize({ width, height: 950 });
        for (const [index, view] of [
          "loom",
          "lineage",
          "section",
          "atlas",
        ].entries()) {
          await page.locator("body").click({ position: { x: 2, y: 2 } });
          await page.keyboard.press(String(index + 1));
          await until(
            async () =>
              (await page.locator("body").getAttribute("data-view")) === view,
            view,
          );
          const dimensions = await page.evaluate(() => ({
            width: innerWidth,
            scroll: document.documentElement.scrollWidth,
          }));
          assert(
            dimensions.scroll <= dimensions.width + 1,
            `${view}/${width} overflow: ${JSON.stringify(dimensions)}`,
          );
          const header = await page.locator(".masthead").boundingBox(),
            tabs = await page
              .getByRole("navigation", { name: "四种可视化视角" })
              .boundingBox();
          assert(
            header.y + header.height <= tabs.y + 1,
            "header must not overlap navigation",
          );
          assert(
            (await page.locator(".execution-dock").boundingBox()).height < 400,
            "mobile controls must remain compact",
          );
          await page.screenshot({
            path: path.join(output, `${width}-${view}.png`),
            fullPage: true,
          });
        }
      }
      await page.setViewportSize({ width: 1440, height: 1000 });
      await page.locator("#x-dim").selectOption("1");
      await page.locator("#y-dim").selectOption("0");
      await page
        .locator("#baseline-select")
        .selectOption(`${project}/transform:uppercase`);
    },
  );
  await check(
    "offline state disables mutations and reconnect restores the console",
    async () => {
      await context.setOffline(true);
      await until(
        async () =>
          (await page.locator(".console-navigation").innerText()).includes(
            "连接中断",
          ),
        "offline",
      );
      assert.equal(await button("新建执行").isEnabled(), false);
      assert.match(
        await page.locator("#persistence-label").innerText(),
        /OFFLINE/,
      );
      await context.setOffline(false);
      await until(() => button("新建执行").isEnabled(), "reconnected");
    },
  );
  await check("stop during a running node cancels at its durable boundary", async () => {
    const id = await launch({text:'running-stop', repeat:5, delay:1});
    await action('停止执行');
    await settled(id, 'cancelled');
  });
  const parallel = catalog.variants.find(v => v.workflowVariant === 'parallel');
  if (parallel) await check('parallel workflow pauses both branches, joins reducer results and completes', async () => {
    const id = await launch({text:'parallel-ui',delay:.2}, {variantId:parallel.id,breakpoints:['left','right']});
    let e = await settled(id,'paused');
    assert.deepEqual(e.nextNodes.sort(), ['left','right']);
    await action('继续运行');
    e = await settled(id,'completed');
    assert.deepEqual(e.state.notes.sort(), ['left','right']);
    assert.deepEqual(e.nodeRuns.filter(n=>n.status==='completed').map(n=>n.nodeId).sort(), ['finish','left','prepare','right']);
  });
  await check(
    "reload preserves executions, branches and real audit history",
    async () => {
      await page.reload();
      await until(() => button("新建执行").isEnabled(), "reloaded");
      const state = await get("/console-state");
      assert(
        state.executions.some((e) => e.id === child && e.parentId === root),
      );
      assert(state.events.length > 0);
      assert.equal(errors.length, 0, errors.join("\n"));
    },
  );
} catch (error) {
  console.error(error);
  process.exitCode = 1;
} finally {
  await writeFile(
    path.join(output, "results.json"),
    JSON.stringify(
      {
        url,
        results,
        pageErrors: errors,
        passed: results.filter((r) => r.passed).length,
        failed: results.filter((r) => !r.passed).length,
      },
      null,
      2,
    ),
  );
  await browser.close();
}
