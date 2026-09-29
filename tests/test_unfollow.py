import json

from people_sync import cli, unfollow


def _rows():
    return [
        {
            "id": "instagram:a",
            "source": "instagram",
            "name": None,
            "handle": "a",
            "i_follow": 1,
            "url": None,
        },
        {
            "id": "instagram:b",
            "source": "instagram",
            "name": None,
            "handle": "b",
            "i_follow": 0,
            "url": "https://www.instagram.com/b/",
        },
        {
            "id": "linkedin:c",
            "source": "linkedin",
            "name": "C D",
            "handle": "c",
            "i_follow": None,
            "url": "https://www.linkedin.com/in/c",
        },
        {
            "id": "partiful:d",
            "source": "partiful",
            "name": "D",
            "handle": "d",
            "i_follow": None,
            "url": None,
        },
    ]


def test_pending_keeps_only_followed_accounts_on_follow_platforms(mocker):
    sql = mocker.patch("people_sync.lifedata.sql", return_value=_rows())
    rows = unfollow.pending()
    assert [r["id"] for r in rows] == ["instagram:a", "linkedin:c"]
    assert rows[0]["url"] == "https://www.instagram.com/a/"
    assert "status = 'ignored'" in sql.call_args.args[0]


def test_cli_lists_by_platform_and_as_json(mocker, capsys):
    mocker.patch("people_sync.lifedata.sql", return_value=_rows())
    cli.main(["unfollow"])
    out = capsys.readouterr().out
    assert "instagram" in out and "https://www.instagram.com/a/" in out and "instagram:b" not in out
    cli.main(["unfollow", "--json"])
    assert [r["id"] for r in json.loads(capsys.readouterr().out)] == ["instagram:a", "linkedin:c"]
