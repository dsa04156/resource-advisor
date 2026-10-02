// Run in a signed-in isolated preview with the two archived Hailo qualification imports.
async (page) => {
  const pattern = "**/api/v1/compute/overview?*";
  const checks = {};
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.locator("#auto").uncheck();
  await page.locator('[data-view="compatibility"]').click();
  await page.getByRole("button", { name: "지금 갱신" }).click();
  await page.getByRole("cell", { name: "정확도 미달", exact: false }).first().waitFor();
  const text = await page.locator("#content").innerText();
  checks.failed_gates_visible = text.includes("원본 일치율 미달") && text.includes("정확도 미달");
  checks.actual_metrics_visible = ["77%", "89%", "66%", "95%"].every((v) => text.includes(v));
  checks.import_provenance_visible = text.includes("외부 실측 가져옴") && text.includes("자동 추천 등록 없음");
  await page.setViewportSize({ width: 390, height: 844 });
  checks.mobile_no_page_overflow = await page.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth + 1,
  );
  await page.setViewportSize({ width: 1280, height: 900 });
  try {
    await page.route(pattern, async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      body.qualifications.items[0].model_name = '<img src=x onerror="window.importInjected=true">';
      await route.fulfill({ response, json: body });
    });
    await page.getByRole("button", { name: "지금 갱신" }).click();
    await page.waitForFunction(() => document.querySelector("#content").textContent.includes("onerror="));
    checks.import_text_escaped = await page.evaluate(
      () => !window.importInjected && !document.querySelector("#content img"),
    );
    await page.unroute(pattern);
    await page.route(pattern, async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      body.qualifications = { page: 0, size: 25, total: 0, has_next: false, items: [] };
      await route.fulfill({ response, json: body });
    });
    await page.getByRole("button", { name: "지금 갱신" }).click();
    await page.waitForFunction(() => document.querySelector("#content").textContent.includes("가져온 검증 기록이 없습니다"));
    checks.empty_not_success = !(await page.locator("#content").innerText()).includes("품질 기준 통과");
  } finally {
    await page.unroute(pattern);
    await page.getByRole("button", { name: "지금 갱신" }).click();
  }
  checks.no_browser_errors = errors.length === 0;
  if (!Object.values(checks).every(Boolean)) throw new Error(JSON.stringify({ checks, errors }));
  return { checks, evidence_kind: "browser UI with archived hardware records" };
};
