/** Starts fresh local WTB and Vite processes, then always closes only those processes. */
import { spawn, execFileSync } from "node:child_process";
import { createServer } from "node:net";
import { createWriteStream } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repo = path.dirname(web);
const output = path.resolve(
  process.env.WTB_E2E_OUTPUT ||
    path.join(repo, "output/playwright", `e2e-${Date.now()}`),
);
await mkdir(output, { recursive: true });
const children = [];
const launch = (command, args, name, cwd, env) => {
  const child = spawn(command, args, {
    cwd,
    env,
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  const stream = createWriteStream(path.join(output, name + ".log"));
  child.stdout.pipe(stream);
  child.stderr.pipe(stream);
  children.push(child);
  child.on("error", (error) => console.error(name, error));
  return child;
};
const port = () =>
  new Promise((resolve, reject) => {
    const server = createServer();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const value = server.address().port;
      server.close(() => resolve(value));
    });
  });
async function ready(url, child) {
  for (let i = 0; i < 200; i++) {
    if (child.exitCode !== null)
      throw Error(`${url}: server exited ${child.exitCode}`);
    try {
      if ((await fetch(url)).ok) return;
    } catch {
      /* server still starting */
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw Error(`${url}: startup timed out, see ${output}`);
}
try {
  const apiPort = await port(),
    webPort = await port();
  const configuredPython = process.env.WTB_E2E_PYTHON;
  const python = configuredPython
    ? /[\\/]/.test(configuredPython)
      ? path.resolve(configuredPython)
      : configuredPython
    : path.join(
        repo,
        ".venv",
        process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
      );
  const env = {
    ...process.env,
    WTB_API_URL: `http://127.0.0.1:${apiPort}`,
    WTB_E2E_URL: `http://127.0.0.1:${webPort}`,
    WTB_E2E_OUTPUT: output,
    PYTHONUNBUFFERED: "1",
  };
  const args = [
    "-m",
    "wtb.api.console",
    "--data-dir",
    path.join(output, "data"),
    "--port",
    String(apiPort),
  ];
  if (process.env.WTB_E2E_CONFIG)
    args.push("--config", path.resolve(process.env.WTB_E2E_CONFIG));
  const backend = launch(python, args, "backend", repo, env);
  await ready(env.WTB_API_URL + "/api/v1/health", backend);
  const frontend = launch(
    process.execPath,
    [
      "node_modules/vite/bin/vite.js",
      ...(process.env.WTB_E2E_PREVIEW ? ["preview"] : []),
      "--host",
      "127.0.0.1",
      "--port",
      String(webPort),
      "--strictPort",
    ],
    "frontend",
    web,
    env,
  );
  await ready(env.WTB_E2E_URL, frontend);
  const test = launch(
    process.execPath,
    ["tests/console-e2e.mjs"],
    "browser",
    web,
    env,
  );
  test.stdout.pipe(process.stdout);
  test.stderr.pipe(process.stderr);
  process.exitCode = await new Promise((resolve) =>
    test.on("exit", (code) => resolve(code ?? 1)),
  );
  await writeFile(
    path.join(output, "environment.json"),
    JSON.stringify(
      {
        python,
        repo,
        api: env.WTB_API_URL,
        url: env.WTB_E2E_URL,
        config: process.env.WTB_E2E_CONFIG || "default",
      },
      null,
      2,
    ),
  );
} catch (error) {
  console.error(error);
  process.exitCode = 1;
} finally {
  for (const child of children.reverse()) {
    if (child.exitCode !== null || !child.pid) continue;
    if (process.platform === "win32") {
      try {
        execFileSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
          windowsHide: true,
          stdio: "ignore",
        });
      } catch {
        /* already exited */
      }
    } else child.kill("SIGTERM");
  }
  console.log("Evidence:", output);
}
