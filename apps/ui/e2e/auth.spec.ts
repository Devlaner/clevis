import { expect, test } from "@playwright/test"

import { E2E_API_BASE } from "../playwright.config"
import { E2E_ADMIN_EMAIL, E2E_ADMIN_PASSWORD } from "./constants"
import { loginAsAdmin } from "./helpers"

test.describe("Login", () => {
  test("valid credentials redirect to the app", async ({ page }) => {
    await loginAsAdmin(page)

    await expect(page.getByRole("heading", { level: 1, name: "Overview" })).toBeVisible()
  })

  test("invalid password shows an error and stays on /login", async ({ page }) => {
    await page.goto("/login")
    await page.getByPlaceholder("you@example.com").fill(E2E_ADMIN_EMAIL)
    await page.getByPlaceholder("Password").fill("definitely-the-wrong-password")
    await page.getByRole("button", { name: "Sign in", exact: true }).click()

    await expect(page.getByText(/invalid credentials/i)).toBeVisible()
    await expect(page).toHaveURL(/\/login/)
  })
})

test.describe("GitHub OAuth error redirect", () => {
  test("shows a clear message for github_oauth_failed and strips the query param", async ({ page }) => {
    await page.goto("/login?error=github_oauth_failed")

    await expect(page.getByText(/GitHub sign-in failed/i)).toBeVisible()
    // The page strips ?error= from the URL once read, so a refresh doesn't re-show it.
    await expect(page).toHaveURL((url) => !url.search.includes("error="))
  })
})

test.describe("Logout", () => {
  test("manual sign-out clears the session and returns to /login", async ({ page }) => {
    await loginAsAdmin(page)
    await expect(page.getByRole("heading", { level: 1, name: "Overview" })).toBeVisible()

    // Sidebar header button shows the user's name (see components/app-sidebar.tsx);
    // clicking it opens ProfileDropdown, whose "Sign out" button calls logout().
    await page.getByRole("button", { name: /E2E Admin/i }).click()
    await page.getByRole("button", { name: "Sign out" }).click()

    await expect(page).toHaveURL(/\/login/)

    // Session should actually be cleared, not just a client-side navigation — reloading a
    // protected route must bounce back to /login rather than showing stale content.
    await page.goto("/security")
    await expect(page).toHaveURL(/\/login/)
  })
})

test.describe("Mid-session 401", () => {
  test("a 401 from the API redirects to /login without a loop", async ({ page }) => {
    await loginAsAdmin(page)
    await expect(page.getByRole("heading", { level: 1, name: "Overview" })).toBeVisible()

    // Force the next API call to look like an expired/revoked session. /audit auto-fetches
    // GET /audit on mount (see app/audit/page.tsx), so navigating there reliably triggers it.
    // Scoped to the API host specifically — a bare "**/audit" pattern would also match the
    // UI's own page navigation request to http://localhost:3000/audit. Trailing "**" so this
    // still matches once query params (e.g. ?limit=100) are appended to the request.
    await page.route(`${E2E_API_BASE}/audit**`, (route) => route.fulfill({ status: 401, body: "{}" }))
    await page.goto("/audit")

    // Regression check for #75/#115: AuthGuard must call logout() (clearing local auth state)
    // before redirecting, otherwise /login's "already authenticated" check bounces straight
    // back to the protected page, which 401s again — an infinite redirect loop.
    await expect(page).toHaveURL(/\/login/, { timeout: 5000 })
    await page.waitForTimeout(1000)
    await expect(page).toHaveURL(/\/login/)
  })
})
