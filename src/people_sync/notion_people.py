"""New person ids, with Notion as an optional anchor.

When a Notion People data source is configured, a new person's life-data id
IS their Notion page id (dash-stripped): that keeps Notion-side relations
(gifts, quotes, trips) resolvable. Without one, the id is minted locally in
the same 32-hex shape and Notion is never contacted.
"""

import os
import uuid

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


def new_person_id(name: str) -> tuple[str, str | None]:
    """(life-data person id, Notion page id or None). Notion only when configured."""
    if not os.environ.get(DATA_SOURCE_ENV):
        return uuid.uuid4().hex, None
    page_id = create_stub(name)
    return page_id.replace("-", ""), page_id
