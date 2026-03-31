from checks.github_checks import BranchProtectionEnabled, OrgMFARequired, SecretScanningEnabled


def run_all_checks(owner: str, token: str) -> dict:
    checks = [OrgMFARequired(), BranchProtectionEnabled(), SecretScanningEnabled()]
    results = []
    for check in checks:
        output = check.run(owner=owner, token=token)
        results.append({
            "id": check.metadata.check_id,
            "title": check.metadata.title,
            "severity": check.metadata.severity,
            "remediation": check.metadata.remediation,
            "status": output["status"],
            "value": output["value"],
        })
    return {"checks": results}
