import assert from "node:assert/strict";
import { chromium } from "playwright";
import { createServer } from "vite";
import { mkdirSync } from "node:fs";
const server = await createServer({
  server: { host: "127.0.0.1", port: 5207, strictPort: true, hmr: false },
});
await server.listen();
const origin = "http://127.0.0.1:5207";
const browser = await chromium.launch({
  headless: true,
  ...(process.platform === "win32" ? { channel: "msedge" } : {}),
});
mkdirSync("artifacts/attendance", { recursive: true });
const add = (date, n) => {
  const d = new Date(date + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
};
try {
  for (const [width, height, dark] of [
    [1024, 640, false],
    [1280, 720, false],
    [1280, 960, false],
    [1680, 1050, true],
    [1920, 1080, false],
    [390, 844, true],
  ]) {
    const context = await browser.newContext({
      viewport: { width, height },
      deviceScaleFactor: width === 1920 ? 1.25 : 1,
    });
    const page = await context.newPage(),
      errors = [],
      writes = [],
      previews = [];
    page.on("pageerror", (e) => errors.push(e.message));
    page.setDefaultTimeout(15000);
    const people = Array.from({ length: 10 }, (_, i) => ({
      id: i + 1,
      name: `合成人员${i + 1}`,
      position: "组员",
      attendance_revision: 0,
    }));
    await context.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      if (url.origin !== origin) return route.abort();
      if (!url.pathname.startsWith("/api/")) return route.continue();
      const send = (body, status = 200) =>
        route.fulfill({
          status,
          contentType: "application/json",
          body: JSON.stringify(body),
        });
      if (url.pathname === "/api/app/bootstrap")
        return send({
          environment: "production",
          timezone: "Asia/Shanghai",
          server_version: "0.31.16",
        });
      if (url.pathname === "/api/auth/me")
        return send({
          user: {
            id: 9001,
            role: "admin",
            permissions: [
              "personnel.basic.view",
              "personnel.sensitive.view",
              "attendance.manage",
            ],
            permission_groups: [],
            display_name: "合成管理员",
          },
        });
      if (url.pathname === "/api/grid-members/departments")
        return send({
          data: [{ id: 1, name: "合成部门", type: "unit", is_active: true }],
        });
      if (url.pathname === "/api/grid-members")
        return send({ data: people, total: 10 });
      if (url.pathname.endsWith("/calendar-options"))
        return send({
          members: people,
          departments: [{ id: 1, name: "合成部门", parent_id: null }],
        });
      if (url.pathname.endsWith("/calendar")) {
        const ids = url.searchParams.getAll("member_ids").map(Number),
          start = url.searchParams.get("start_date"),
          end = url.searchParams.get("end_date");
        const leaves = [
          {
            id: 11,
            member_id: 1,
            absence_type: "temporary_leave",
            start_date: add(start, 7),
            end_date: add(start, 16),
            source: "manual",
            workflow_ticket_id: null,
          },
        ];
        const days = [];
        for (const id of ids)
          for (let d = start; d <= end; d = add(d, 1)) {
            const weekday = new Date(d + "T00:00:00Z").getUTCDay();
            const leave =
              id === 1 && d >= leaves[0].start_date && d <= leaves[0].end_date;
            days.push({
              member_id: id,
              date: d,
              state: leave
                ? "leave"
                : weekday === 0 || weekday === 6
                  ? "rest"
                  : "work",
              source: "default",
              leave_id: leave ? 11 : null,
              conflict: false,
              overlapping: false,
            });
          }
        return send({
          editable: true,
          business_today: new Date().toISOString().slice(0, 10),
          members: people.filter((p) => ids.includes(p.id)),
          days,
          leaves,
        });
      }
      if (url.pathname.endsWith("/history"))
        return send({ data: [], total: 0 });
      if (url.pathname.endsWith("/preview")) {
        previews.push(route.request().postDataJSON());
        return send({
          message: "合成预览",
          changes: [
            { member_id: 1, date: "2026-10-19", before: "work", after: "rest" },
          ],
        });
      }
      if (
        url.pathname.endsWith("/schedule") ||
        url.pathname.endsWith("/leave")
      ) {
        writes.push(route.request().postDataJSON());
        return send({ message: "合成保存" });
      }
      return send({ data: [] });
    });
    await page.goto(
      origin + "/tests/attendanceCalendar.fixture.html" + (dark ? "?dark" : ""),
    );
    await page.getByText("请先选择需要查看的人员").waitFor();
    await page
      .locator(".ant-select")
      .filter({ hasText: "搜索并多选人员（最多 100 人）" })
      .locator("input")
      .click();
    for (let i = 1; i <= 10; i++)
      await page
        .locator(`.ant-select-item-option[title="合成人员${i}"]`)
        .click();
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "安排上班／休息" }).waitFor();
    await page.getByRole("button", { name: "安排上班／休息" }).click();
    await page.getByRole("textbox", { name: "调整原因" }).fill("合成测试调整");
    await page.getByRole("button", { name: "预览影响", exact: true }).click();
    await page.getByText("请核对后确认").waitFor();
    assert.equal(writes.length, 0);
    await page
      .getByRole("button", { name: "取消调整", exact: true })
      .last()
      .click();
    await page.locator(".ant-modal-wrap").waitFor({ state: "hidden" });
    assert.equal(writes.length, 0);
    await page.getByRole("button", { name: "安排上班／休息" }).click();
    await page.getByRole("textbox", { name: "调整原因" }).fill("合成测试确认");
    await page.getByRole("button", { name: "预览影响", exact: true }).click();
    await page.getByText("请核对后确认").waitFor();
    await page.getByRole("button", { name: "确认提交", exact: true }).click();
    await page
      .getByText(
        "已更新；动态统计按最新安排计算，已保存工作日志及导出文件需显式刷新或重新生成。",
      )
      .waitFor();
    await page.locator(".ant-modal-wrap").waitFor({ state: "hidden" });
    assert.equal(writes.length, 1);
    assert.equal(previews.length, 2);
    assert.equal(writes[0].member_ids.length, 10);
    if (width === 1280 && height === 960) {
      const source = page
        .locator(".attendance-bar.attendance-work[draggable=true]")
        .first();
      const target = page.locator(".attendance-week-dates button").nth(3);
      await source.dragTo(target);
      await page
        .getByRole("textbox", { name: "调整原因" })
        .fill("合成拖动测试");
      await page.getByRole("button", { name: "预览影响", exact: true }).click();
      await page.getByText("请核对后确认").waitFor();
      assert.ok(previews.at(-1).original_start);
      assert.ok(previews.at(-1).original_end);
      assert.equal(writes.length, 1);
      await page.getByRole("button", { name: "取消调整", exact: true }).click();
      await page.locator(".ant-modal-wrap").waitFor({ state: "hidden" });
      // Real HTML5 end-handle drag modifies the original leave, not a new one.
      const handle = page
        .locator('.attendance-bar.attendance-leave [aria-label="拖动结束日期"]')
        .first();
      await handle.dragTo(
        page.locator(".attendance-week-dates button").nth(18),
      );
      await page
        .getByRole("textbox", { name: "调整原因" })
        .fill("合成请假调整");
      await page.getByRole("button", { name: "预览影响", exact: true }).click();
      await page.getByText("请核对后确认").waitFor();
      assert.equal(previews.at(-1).record_id, 11);
      assert.equal(previews.at(-1).action, "correct");
      assert.equal(writes.length, 1);
      await page.getByRole("button", { name: "取消调整", exact: true }).click();
      await page.locator(".ant-modal-wrap").waitFor({ state: "hidden" });
      await page.locator(".attendance-week-dates button").first().click();
      await page.getByText("全部安排", { exact: false }).waitFor();
      assert.equal(
        await page.locator(".ant-drawer-body .attendance-bar").count(),
        10,
      );
      await page.locator(".ant-drawer-close").click();
      await page.locator(".ant-drawer-mask").waitFor({ state: "hidden" });
      // Keep a real native drag active while selecting a date in the next month.
      const leaveBar = page
        .locator(".attendance-bar.attendance-leave[draggable=true]")
        .first();
      await leaveBar.scrollIntoViewIfNeeded();
      const bounds = await leaveBar.boundingBox();
      await page.mouse.move(
        bounds.x + bounds.width / 2,
        bounds.y + bounds.height / 2,
      );
      await page.mouse.down();
      await page.mouse.move(
        bounds.x + bounds.width / 2 + 12,
        bounds.y + bounds.height / 2,
        { steps: 4 },
      );
      const nextMonth = page.getByRole("button", { name: "拖入切换下月" });
      await nextMonth.waitFor();
      const nav = await nextMonth.boundingBox();
      await page.mouse.move(nav.x + nav.width / 2, nav.y + nav.height / 2, {
        steps: 8,
      });
      const destination = page
        .locator(".attendance-drag-targets button")
        .nth(2);
      await destination.waitFor();
      const dest = await destination.boundingBox();
      await page.mouse.move(dest.x + dest.width / 2, dest.y + dest.height / 2, {
        steps: 8,
      });
      await page.mouse.up();
      await page
        .getByRole("textbox", { name: "调整原因" })
        .fill("合成跨月调整");
      await page.getByRole("button", { name: "预览影响", exact: true }).click();
      await page.getByText("请核对后确认").waitFor();
      assert.equal(previews.at(-1).record_id, 11);
      assert.equal(writes.length, 1);
      await page.getByRole("button", { name: "取消调整", exact: true }).click();
      await page.locator(".ant-modal-wrap").waitFor({ state: "hidden" });
    }
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth + 1,
    );
    assert.equal(overflow, false);
    assert.deepEqual(errors, []);
    await page.screenshot({
      path: `artifacts/attendance/${width}-${height}-${dark ? "dark" : "light"}.png`,
      fullPage: true,
    });
    console.log(JSON.stringify({ width, height, dark, result: "passed" }));
    await context.close();
  }
} finally {
  await browser.close();
  await server.close();
}
