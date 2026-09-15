"""Notion People stub-page creation - the row-id invariant made executable.

Every life-data person id IS their Notion People page id (dash-stripped),
which is what keeps Notion-side Gifts/Quotes/Trips relations resolvable.
Callers create the stub page here, then insert the matching people row with
the dash-stripped id.
"""

import os

import httpx

DATA_SOURCE_ENV = "PEOPLE_SYNC_NOTION_PEOPLE_DS"
MISSING_DS_MSG = f"{DATA_SOURCE_ENV} is not set - which Notion data source holds People?"
_API = "https://api.notion.com/v1/pages"
MISSING_TOKEN_MSG = "NOTION_API_TOKEN is not set - cannot create a Notion People stub page"


def create_stub(name: str) -> str:
    data_source = os.environ.get(DATA_SOURCE_ENV)
    if not data_source:
        raise RuntimeError(MISSING_DS_MSG)
    token = os.environ.get("NOTION_API_TOKEN")
    if not token:
        raise RuntimeError(MISSING_TOKEN_MSG)
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2026-03-11",
        "Content-Type": "application/json",
    }
    body = {
        "parent": {"type": "data_source_id", "data_source_id": data_source},
        "properties": {"title": {"title": [{"text": {"content": name}}]}},
    }
    resp = httpx.post(_API, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    return resp.json()["id"]
