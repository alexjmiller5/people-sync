from contact_sync import notion_people


class _Resp:
    def __init__(self, json_data=None, status_code=200):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")


def test_create_stub_posts_title_only_to_data_source(mocker, monkeypatch):
    monkeypatch.setenv("NOTION_API_TOKEN", "test-token")
    post = mocker.patch(
        "contact_sync.notion_people.httpx.post",
        return_value=_Resp(json_data={"id": "1a80-3953-a8af-80ab-000bfe407316"}),
    )

    page_id = notion_people.create_stub("Test Person")

    assert page_id == "1a80-3953-a8af-80ab-000bfe407316"
    url = post.call_args.args[0]
    kwargs = post.call_args.kwargs
    assert url == "https://api.notion.com/v1/pages"
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"
    assert kwargs["headers"]["Notion-Version"] == "2026-03-11"
    body = kwargs["json"]
    assert body["parent"] == {
        "type": "data_source_id",
        "data_source_id": notion_people.DATA_SOURCE_ID,
    }
    assert body["properties"] == {"Name": {"title": [{"text": {"content": "Test Person"}}]}}
