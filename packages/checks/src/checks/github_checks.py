import httpx

from checks.base import Check, CheckMetadata


def _get(url: str, token: str):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    with httpx.Client(timeout=20) as client:
        r = client.get(url, headers=headers)
    r.raise_for_status()
    return r.json()


class OrgMFARequired(Check):
    metadata = CheckMetadata(
        check_id="organization_members_mfa_required",
        title="Organization requires 2FA/MFA",
        severity="high",
        remediation="Require two-factor authentication for all org members in org settings.",
    )

    def run(self, owner: str, token: str) -> dict:
        org = _get(f"https://api.github.com/orgs/{owner}", token)
        enabled = bool(org.get("two_factor_requirement_enabled", False))
        return {"status": "pass" if enabled else "fail", "value": enabled}


class BranchProtectionEnabled(Check):
    metadata = CheckMetadata(
        check_id="repository_default_branch_protection_enabled",
        title="Default branch has protection rules",
        severity="high",
        remediation="Enable branch protection for default branch on all active repositories.",
    )

    def run(self, owner: str, token: str) -> dict:
        repos = _get(f"https://api.github.com/orgs/{owner}/repos?per_page=100", token)
        checked = 0
        protected = 0
        for repo in repos:
            checked += 1
            branch = repo.get("default_branch")
            details = _get(f"https://api.github.com/repos/{owner}/{repo['name']}/branches/{branch}", token)
            if details.get("protected"):
                protected += 1
        compliant = checked > 0 and checked == protected
        return {"status": "pass" if compliant else "fail", "value": {"checked": checked, "protected": protected}}


class SecretScanningEnabled(Check):
    metadata = CheckMetadata(
        check_id="repository_secret_scanning_enabled",
        title="Secret scanning enabled",
        severity="medium",
        remediation="Enable secret scanning for repositories where available.",
    )

    def run(self, owner: str, token: str) -> dict:
        repos = _get(f"https://api.github.com/orgs/{owner}/repos?per_page=100", token)
        enabled = 0
        total = len(repos)
        for repo in repos:
            sec = _get(f"https://api.github.com/repos/{owner}/{repo['name']}", token).get("security_and_analysis", {})
            if sec.get("secret_scanning", {}).get("status") == "enabled":
                enabled += 1
        compliant = total > 0 and enabled == total
        return {"status": "pass" if compliant else "fail", "value": {"enabled": enabled, "total": total}}
