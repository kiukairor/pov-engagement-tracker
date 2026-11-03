"""Minimal NerdGraph client. Reads NR_API_KEY / NR_ACCOUNT_ID / NR_REGION from env or ./.env."""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent


def _load_env():
    # PENGAGE_ENV points at a per-account settings file (accounts/<id>.env) so one account's key
    # never carries over to another. Real env vars still win over the file.
    env_file = Path(os.environ["PENGAGE_ENV"]) if os.environ.get("PENGAGE_ENV") else HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()
API_KEY = os.environ.get("NR_API_KEY") or os.environ.get("NEW_RELIC_API_KEY")
# No default account: a missing NR_ACCOUNT_ID must never silently fall back to another prospect's account.
ACCOUNT_ID = int(os.environ["NR_ACCOUNT_ID"]) if os.environ.get("NR_ACCOUNT_ID") else None
ENDPOINT = ("https://api.eu.newrelic.com/graphql" if os.environ.get("NR_REGION", "US").upper() == "EU"
            else "https://api.newrelic.com/graphql")


def graphql(query, variables=None):
    """Read-only: mutations are always refused (this copy never writes to the account)."""
    if not API_KEY:
        sys.exit("Set NR_API_KEY (env var or .env next to this script)")
    if ACCOUNT_ID is None:
        sys.exit("Set NR_ACCOUNT_ID (env var or .env next to this script); there is no default account")
    if query.lstrip().lower().startswith("mutation"):
        raise PermissionError("Refusing to send a NerdGraph mutation: this tool is read-only")
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"API-Key": API_KEY, "Content-Type": "application/json"},
    )
    for attempt in range(3):  # transient network timeouts happen on long runs
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))


def nrql(q, account_id=None, timeout=90):
    """Run NRQL and return the results list. Raises on GraphQL errors."""
    res = graphql(
        "query($a:Int!,$q:Nrql!,$t:Seconds){actor{account(id:$a){nrql(query:$q,timeout:$t){results}}}}",
        {"a": account_id or ACCOUNT_ID, "q": q, "t": timeout},
    )
    if res.get("errors"):
        raise RuntimeError(f"{res['errors'][0].get('message')} :: {q}")
    return res["data"]["actor"]["account"]["nrql"]["results"]


if __name__ == "__main__":
    print(json.dumps(nrql(" ".join(sys.argv[1:])), indent=1))
