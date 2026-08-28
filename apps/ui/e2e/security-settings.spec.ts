import { expect, test } from "@playwright/test"

import { E2E_API_BASE } from "../playwright.config"
import { loginAsAdmin } from "./helpers"

test.describe("Settings — org connection surface", () => {
  test("shows the GitHub App install path after login", async ({ page }) => {
    await loginAsAdmin(page)
    await page.goto("/settings")

    // Org-connection UI lives on Settings — real GitHub OAuth can't run in CI,
    // so this asserts the install surface is reachable and wired (issue #187).
    await expect(page.getByText("Connected GitHub accounts")).toBeVisible()
    await expect(
      page.getByText(/No accounts connected yet|Install the Clevis GitHub App/i),
    ).toBeVisible()

    // Install button is present when NEXT_PUBLIC_GITHUB_APP_SLUG is set in the
    // stack; otherwise the page explains how to enable it. Either is fine — both
    // prove the connection section rendered instead of a stub.
    const installButton = page.getByRole("button", { name: /Install GitHub App/i })
    const missingSlugHint = page.getByText(/GitHub App integration isn.t set up/i)
    await expect(installButton.or(missingSlugHint)).toBeVisible()
  })
})

test.describe("Security scan", () => {
  test("runs a scan happy path without a real GitHub org (API mocked)", async ({ page }) => {
    await loginAsAdmin(page)

    await page.route(`${E2E_API_BASE}/me/analytics/overview`, async (route) => {
      if (route.request().method() !== "POST") {
        await route.continue()
        return
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          owner: "acme",
          score: 100,
          total_checks: 1,
          failed_checks: 0,
          repo_count: 3,
          checks: [
            {
              id: "organization_members_mfa_required",
              title: "MFA required for org members",
              status: "pass",
              severity: "high",
              value: true,
              remediation: "Require two-factor authentication for all organization members.",
            },
          ],
        }),
      })
    })

    await page.goto("/security")
    await expect(page.getByRole("heading", { level: 1, name: "Health & Security" })).toBeVisible()

    await page.getByPlaceholder("e.g. octocat").fill("acme")
    await page.getByRole("button", { name: "Run scan" }).click()

    await expect(page.getByText("Results — acme")).toBeVisible()
    await expect(page.getByText("MFA required for org members")).toBeVisible()
    await expect(page.getByLabel(/Security score: 100/i)).toBeVisible()
  })

  test("surfaces a clear error when the API has no installation token", async ({ page }) => {
    await loginAsAdmin(page)

    await page.route(`${E2E_API_BASE}/me/analytics/overview`, async (route) => {
      if (route.request().method() !== "POST") {
        await route.continue()
        return
      }
      await route.fulfill({
        status: 400,
        contentType: "application/json",
        body: JSON.stringify({
          detail:
            "No GitHub App installation found for 'missing-org' and no token was provided. " +
            "Install the GitHub App for this organization in Settings → Connected orgs.",
        }),
      })
    })

    await page.goto("/security")
    await page.getByPlaceholder("e.g. octocat").fill("missing-org")
    await page.getByRole("button", { name: "Run scan" }).click()

    await expect(page.getByTestId("scan-error")).toContainText(/No GitHub App installation found for 'missing-org'/i)
  })
})
