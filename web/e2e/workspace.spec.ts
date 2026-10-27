import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("/");
  await page
    .getByLabel("管理员密码", { exact: true })
    .fill("browser-test-password");
  await page.getByRole("button", { name: "登录工作台" }).click();
  await expect(
    page.getByRole("heading", { name: "总览", exact: true }),
  ).toBeVisible();
});

test("all pages, account sync, details, and narrow layout", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.screenshot({
    path: "../test-results/overview.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "账号管理", exact: true }).click();
  await expect(
    page.getByRole("cell", { name: "123.4568", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "同步", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("已同步");
  await page
    .getByRole("button", { name: "演示账号 · 自动化测试", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "积分包", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "关闭窗口" }).click();
  for (const name of [
    "账号池",
    "模型中心",
    "API Keys",
    "用量与日志",
    "系统设置",
  ]) {
    await page.getByRole("button", { name, exact: true }).click();
    await expect(
      page.getByRole("heading", { name, exact: true }),
    ).toBeVisible();
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "总览", exact: true }).click();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({ path: "../test-results/mobile.png", fullPage: true });
  expect(errors).toEqual([]);
});

test("create key, real HTTP gateway call, log detail and export", async ({
  page,
  request,
}) => {
  await page.getByRole("button", { name: "API Keys", exact: true }).click();
  await page.getByRole("button", { name: "创建 API Key", exact: true }).click();
  await page.getByLabel("名称", { exact: true }).fill("浏览器验收");
  await page.getByLabel("总请求预算（留空不限）").fill("5");
  await page.getByRole("button", { name: "创建密钥", exact: true }).click();
  await expect(page.getByRole("heading", { name: "密钥已创建" })).toBeVisible();
  const key = await page.locator(".secret").innerText();
  await page.getByRole("button", { name: "我已保存" }).click();
  const result = await request.post("/v1/chat/completions", {
    headers: { Authorization: "Bearer " + key },
    data: {
      model: "test-model",
      messages: [{ role: "user", content: "测试" }],
    },
  });
  expect(result.ok()).toBe(true);
  expect((await result.json()).choices[0].message.content).toBe("连接成功。");
  await page.getByRole("button", { name: "刷新数据" }).click();
  await page.getByRole("button", { name: "用量与日志", exact: true }).click();
  await expect(
    page.getByRole("cell", { name: "成功", exact: true }),
  ).toBeVisible();
  const download = page.waitForEvent("download");
  await page.getByRole("link", { name: "导出 CSV" }).click();
  expect((await download).suggestedFilename()).toBe("requests.csv");
  await page.locator("tbody .text-link").first().click();
  await expect(page.getByRole("heading", { name: "请求详情" })).toBeVisible();
  await expect(page.locator("pre")).toContainText("ledger");
});

test("account import, pool edit, model alias and settings", async ({
  page,
}) => {
  await page.getByRole("button", { name: "账号管理", exact: true }).click();
  await page.getByRole("button", { name: "添加账号", exact: true }).click();
  await page.locator("summary").click();
  await page.getByLabel("账号名称", { exact: true }).fill("兼容上游测试");
  await page
    .getByLabel("连接器", { exact: true })
    .selectOption("openai_compatible");
  await page
    .getByLabel("授权 JSON（仅用于此账号导入）")
    .fill(
      JSON.stringify({
        endpoint: "http://127.0.0.1:19999/v1",
        api_key: "mock-key",
      }),
    );
  await page.getByRole("button", { name: "导入并同步" }).click();
  await expect(
    page.getByRole("button", { name: "兼容上游测试", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "账号池", exact: true }).click();
  await page.getByRole("button", { name: "创建账号池", exact: true }).click();
  await page.getByLabel("名称", { exact: true }).fill("开发池");
  await page.getByLabel("调度策略").selectOption("expiring");
  await page.getByRole("checkbox", { name: /兼容上游测试/ }).check();
  await page.getByRole("button", { name: "保存账号池", exact: true }).click();
  await expect(page.getByRole("heading", { name: "开发池" })).toBeVisible();
  await page.getByRole("button", { name: "模型中心", exact: true }).click();
  await page.getByRole("button", { name: "添加模型别名" }).click();
  await page.getByLabel("客户端模型名称").fill("daily");
  await page.getByRole("button", { name: "保存映射" }).click();
  await expect(page.locator(".alias-row")).toContainText("daily");
  await page.getByRole("button", { name: "系统设置", exact: true }).click();
  await page.getByLabel("余额与模型同步间隔（秒）").fill("600");
  await page.getByRole("button", { name: "保存设置" }).click();
  await expect(page.getByLabel("余额与模型同步间隔（秒）")).toHaveValue("600");
});

test("encrypted backup download and restore to fresh directory", async ({
  page,
}) => {
  await page.getByRole("button", { name: "系统设置", exact: true }).click();
  await page.getByRole("button", { name: "创建加密备份" }).click();
  await page
    .getByLabel("备份密码（至少 12 位）")
    .fill("browser-backup-passphrase");
  const downloading = page.waitForEvent("download");
  await page.getByRole("button", { name: "加密并下载" }).click();
  const download = await downloading;
  const file = await download.path();
  await page.getByRole("button", { name: "恢复备份到新目录" }).click();
  await page.getByLabel("加密备份文件").setInputFiles(file!);
  await page
    .getByLabel("备份密码", { exact: true })
    .fill("browser-backup-passphrase");
  await page.getByRole("button", { name: "校验并恢复到新目录" }).click();
  await expect(page.getByRole("alert")).toContainText("恢复完成", {
    timeout: 15000,
  });
});

test("OAuth browser flow uses official URL and completes polling", async ({
  page,
}) => {
  await page.getByRole("button", { name: "账号管理", exact: true }).click();
  await page.getByRole("button", { name: "添加账号", exact: true }).click();
  await page.getByRole("button", { name: "扫码授权 WorkBuddy 国内版" }).click();
  await expect(page.getByRole("img", { name: "官方登录二维码" })).toBeVisible();
  await expect(
    page.getByRole("link", { name: "打开官方授权页面" }),
  ).toHaveAttribute(
    "href",
    "https://www.codebuddy.cn/login?state=fixture-state",
  );
  await expect(page.getByRole("alert")).toContainText("授权完成", {
    timeout: 15000,
  });
});
