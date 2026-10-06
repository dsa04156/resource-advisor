"""Import an immutable report using a private HTTPS/project-operator config.

The same report can be retried after a failed HTTP response. No compute occurs.
"""

import argparse
import json
import ssl
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from resource_advisor.passive import PassiveImport


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--api-config", type=Path, required=True)
    args = parser.parse_args()
    report = PassiveImport.model_validate_json(args.report.read_text())
    config = json.loads(args.api_config.read_text())
    url = config["api_url"].rstrip("/")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("a credential-free HTTPS API origin is required")
    tls = ssl.create_default_context(cafile=config.get("ca_file"))
    with httpx.Client(verify=tls, timeout=30, follow_redirects=False) as client:
        response = client.post(
            url + "/api/v1/compute/observations",
            headers={"Authorization": "Bearer " + config["operator_token"]},
            json=report.model_dump(mode="json"),
        )
        response.raise_for_status()
        print(json.dumps(response.json(), indent=2))


if __name__ == "__main__":
    main()
