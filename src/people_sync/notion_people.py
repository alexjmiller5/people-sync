"""Notion People stub-page creation - the row-id invariant made executable.

Every life-data person id IS their Notion People page id (dash-stripped),
which is what keeps Notion-side Gifts/Quotes/Trips relations resolvable.
Callers create the stub page here, then insert the matching people row with
the dash-stripped id.
"""

import os

import httpx

DATA_SOURCE_ID = "1a803953-a8af-80ab-824d-000bfe407316"
_API = "https://api.notion.com/v1/pages"
MISSING_TOKEN_MSG = "NOTION_API_TOKEN is not set - cannot create a Notion People stub page"


def create_stub(name: str) -> str:
    token = os.environ.get("NOTION_API_TOKEN")
    if not token:
        raise RuntimeError(MISSING_TOKEN_MSG)
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2026-03-11",
        "Content-Type": "application/json",
    }
    body = {
        "parent": {"type": "data_source_id", "data_source_id": DATA_SOURCE_ID},
        "properties": {"Name": {"title": [{"text": {"content": name}}]}},
    }
    resp = httpx.post(_API, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    return resp.json()["id"]
