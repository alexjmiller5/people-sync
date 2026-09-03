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


def create_stub(name: str) -> str:
    headers = {
        "Authorization": f"Bearer {os.environ['NOTION_API_TOKEN']}",
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
