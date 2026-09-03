"""One-off: snapshot the Notion People DB to data/notion_people_snapshot.json."""

import json
import os
import pathlib
import httpx

DS = "1a803953-a8af-80ab-824d-000bfe407316"
H = {
    "Authorization": f"Bearer {os.environ['NOTION_API_TOKEN']}",
    "Notion-Version": "2026-03-11",
    "Content-Type": "application/json",
}


def flatten(props: dict) -> dict:
    out = {}
    for key, p in props.items():
        t = p["type"]
        if t in ("title", "rich_text"):
            out[key] = "".join(x["plain_text"] for x in p[t]) or None
        elif t == "select":
            out[key] = p[t]["name"] if p[t] else None
        elif t == "multi_select":
            out[key] = [x["name"] for x in p[t]]
        elif t == "date":
            out[key] = p[t]["start"] if p[t] else None
        elif t == "checkbox":
            out[key] = p[t]
        elif t == "url":
            out[key] = p[t]
    return out


pages, cursor = {}, None
with httpx.Client(timeout=30) as c:
    while True:
        body = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
        r = c.post(f"https://api.notion.com/v1/data_sources/{DS}/query", headers=H, json=body)
        r.raise_for_status()
        d = r.json()
        for pg in d["results"]:
            pages[pg["id"].replace("-", "")] = {
                "last_edited_time": pg["last_edited_time"],
                **flatten(pg["properties"]),
            }
        if not d.get("has_more"):
            break
        cursor = d["next_cursor"]
pathlib.Path("data/notion_people_snapshot.json").write_text(json.dumps(pages, indent=1))
print(f"{len(pages)} pages")
