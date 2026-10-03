// Playwright CLI run-code, on an isolated signed-in console with Slurm inventory.
// Faults are browser-local response overrides, not physical scheduler evidence.
async (page) => {
  const checks = {};
  const pattern = "**/api/v1/compute/overview?*";
  const refresh = async () => {
    const response = page.waitForResponse(r => r.url().includes("/api/v1/compute/overview?"));
    await page.locator("#refresh").click();
    await (await response).finished();
    await page.waitForFunction(() => !document.querySelector("#refresh").disabled);
  };
  const panel = () => page.locator("section.panel").filter({
    has: page.getByRole("heading", { name: /lab-slurm · Slurm/ }),
  });
  try {
    await page.locator("#auto").uncheck();
    await page.locator('[data-view="execution"]').click();
    await refresh();
    await panel().getByText("Slurm: 연결 미설정", { exact: true }).first().waitFor();
    checks.host_meters = await panel().locator("meter").count() === 4;
    checks.no_ready_claim = !(await panel().textContent()).includes("Ready");
    checks.unknown_accelerator = await panel().getByText("가속기 등록 상태 확인 불가", {exact: true}).count() === 2;
    await page.locator('[data-view="history"]').click();
    await page.getByText("Slurm 큐 상태: 연결 미설정", {exact: true}).waitFor();
    checks.unknown_queue = await page.getByText("Slurm 큐 상태: 연결 미설정", {exact: true}).isVisible();
    await page.locator('[data-view="execution"]').click();
    await panel().waitFor();
    await page.setViewportSize({width: 390, height: 844});
    checks.mobile_no_page_overflow = await page.evaluate(() => document.documentElement.scrollWidth === innerWidth);
    await page.screenshot({path: "output/playwright/slurm-mobile.png", fullPage: true});

    await page.route(pattern, async (route) => {
      const response = await route.fetch(), body = await response.json();
      for (const s of body.inventory) if (s.backend === "slurm") {
        for (const sample of Object.values(s.nodes[0].telemetry)) {
          sample.status = "unavailable"; sample.value = null;
        }
      }
      await route.fulfill({response, json: body});
    });
    await refresh();
    checks.one_host_outage_keeps_peer = await panel().locator("meter").count() === 2;
    await page.unroute(pattern);
    await page.route(pattern, async (route) => {
      const response = await route.fetch(), body = await response.json();
      for (const s of body.inventory) if (s.backend === "slurm") s.collected_at = new Date(Date.now() - 600000).toISOString();
      await route.fulfill({response, json: body});
    });
    await refresh();
    checks.stale_hides_meters = await panel().locator("meter").count() === 0;
    await page.unroute(pattern);
    await page.route(pattern, async (route) => {
      const response = await route.fetch(), body = await response.json();
      const stamp = new Date().toISOString(); body.generated_at = stamp;
      for (const s of body.inventory) if (s.backend === "slurm") {
        s.collected_at = stamp; s.stale_after_seconds = 5;
        for (const n of s.nodes) for (const sample of Object.values(n.telemetry)) sample.observed_at = stamp;
      }
      await route.fulfill({response, json: body});
    });
    await refresh();
    if (await panel().locator("meter").count() !== 4) throw new Error("fresh sample not shown");
    await page.waitForFunction(() => [...document.querySelectorAll("section.panel")].filter(p => p.querySelector("h2")?.textContent.includes("lab-slurm · Slurm")).every(p => !p.querySelector("meter")), null, {timeout: 10000});
    checks.expires_without_polling = true;
    await page.unroute(pattern);
    await page.route(pattern, async (route) => {
      const response = await route.fetch(), body = await response.json();
      const s = body.inventory.find(s => s.backend === "slurm");
      s.nodes[0].node_ref = '<img src=x onerror="window.slurmInjected=true">';
      await route.fulfill({response, json: body});
    });
    await refresh();
    checks.inert_labels = await page.evaluate(() => document.querySelector("#content").textContent.includes("slurmInjected") && !window.slurmInjected && !document.querySelector("#content img"));
    if (Object.values(checks).some(v => v !== true)) throw new Error(JSON.stringify(checks));
    return checks;
  } finally {
    await page.unroute(pattern);
    await refresh();
  }
}
