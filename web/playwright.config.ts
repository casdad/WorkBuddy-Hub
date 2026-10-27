import { defineConfig } from "@playwright/test";
import { existsSync } from "node:fs";
const python = [
  "../.venv/Scripts/python.exe",
  "../../.venv/Scripts/python.exe",
].find(existsSync);
if (!python)
  throw new Error("请在项目目录建立 .venv 并安装 requirements.lock.txt");
export default defineConfig({
  testDir: "./e2e",
  workers: 1,
  retries: 0,
  timeout: 30000,
  reporter: [
    ["list"],
    ["json", { outputFile: "../test-results/browser.json" }],
  ],
  use: {
    channel: "msedge",
    baseURL: "http://127.0.0.1:18792",
    viewport: { width: 1440, height: 1000 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: {
    command: `"${python}" -m uvicorn tests.web_server:app --app-dir .. --host 127.0.0.1 --port 18792 --log-level warning`,
    url: "http://127.0.0.1:18792/healthz",
    reuseExistingServer: false,
    timeout: 30000,
  },
});
