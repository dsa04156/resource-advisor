// UI fault checks for an isolated Playwright CLI session already signed in to /console.
// Route overrides are browser-local; this does not modify the collector or backend.
async (page) => {
  const checks = {};
  const pattern = "**/api/v1/compute/overview?*";
  try {
    await page.locator("#auto").uncheck();
    await page.getByRole("link", { name: "실행 현황", exact: true }).click();
    await page.getByRole("button", { name: "지금 갱신" }).click();
    await page.waitForFunction(
      () => document.querySelectorAll("meter").length > 0,
    );
    checks.live_meters = await page.locator("meter").count();
    await page.route(pattern, (route) => route.abort("failed"));
    await page.getByRole("button", { name: "지금 갱신" }).click();
    await page.waitForFunction(() =>
      document.querySelector("#notice").textContent.includes("자원 API"),
    );
    checks.outage_hides_meters = (await page.locator("meter").count()) === 0;
    await page.unroute(pattern);
    await page.route(pattern, async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      for (const s of body.inventory)
        s.collected_at = new Date(Date.now() - 600000).toISOString();
      await route.fulfill({ response, json: body });
    });
    await page.getByRole("button", { name: "지금 갱신" }).click();
    await page.waitForFunction(() =>
      document.querySelector("#content").textContent.includes("오래된 값"),
    );
    checks.stale_hides_meters = (await page.locator("meter").count()) === 0;
    await page.unroute(pattern);
    await page.route(pattern, async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      const timestamp = new Date().toISOString();
      body.generated_at = timestamp;
      function renew(value) {
        if (value && typeof value === "object") {
          if ("observed_at" in value) value.observed_at = timestamp;
          Object.values(value).forEach(renew);
        }
      }
      for (const s of body.inventory) {
        renew(s);
        s.collected_at = timestamp;
        s.stale_after_seconds = 5;
      }
      await route.fulfill({ response, json: body });
    });
    await page.getByRole("button", { name: "지금 갱신" }).click();
    await page.waitForFunction(
      () => document.querySelectorAll("meter").length > 0,
    );
    await page.waitForFunction(
      () => document.querySelectorAll("meter").length === 0,
      null,
      { timeout: 10000 },
    );
    checks.expiry_without_polling = true;

    await page.unroute(pattern);
    await page.route(pattern, async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      body.inventory[0].nodes[0].node_ref =
        '<img src=x onerror="window.consoleInjected=true">';
      await route.fulfill({ response, json: body });
    });
    await page.getByRole("button", { name: "지금 갱신" }).click();
    await page.waitForFunction(() =>
      document
        .querySelector("#content")
        .textContent.includes("consoleInjected"),
    );
    checks.untrusted_text_is_inert = await page.evaluate(
      () => !window.consoleInjected && !document.querySelector("#content img"),
    );
    checks.mobile_no_page_overflow = await page.evaluate(
      () => document.documentElement.scrollWidth === innerWidth,
    );
    await page.unroute(pattern);
    await page.getByRole("button", { name: "연결 해제", exact: true }).click();
    checks.logout_clears_data =
      (await page.locator("#workspace").isHidden()) &&
      (await page.locator("#content").textContent()) === "";
    checks.no_persistent_credentials = await page.evaluate(
      () =>
        localStorage.length === 0 &&
        sessionStorage.length === 0 &&
        document.querySelector("#token").value === "",
    );
    if (Object.entries(checks).some(([k, v]) => k !== "live_meters" && !v))
      throw new Error(JSON.stringify(checks));
    return checks;
  } finally {
    await page.unroute(pattern);
    if (
      await page
        .getByRole("button", { name: "연결 해제", exact: true })
        .isVisible()
    )
      await page
        .getByRole("button", { name: "연결 해제", exact: true })
        .click();
  }
}
