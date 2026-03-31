from dataclasses import dataclass


@dataclass
class CheckMetadata:
    check_id: str
    title: str
    severity: str
    remediation: str


class Check:
    metadata: CheckMetadata

    def run(self, owner: str, token: str) -> dict:
        raise NotImplementedError
