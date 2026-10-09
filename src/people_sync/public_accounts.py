"""Explicitly resolve a social ledger record outside personal relationships."""

import sys

from people_sync import somadata

OWNERS = {
    "organization": ("organizations", "organization_id"),
    "figure": ("public_figures", "public_figure_id"),
    "festival": ("music_festivals", "music_festival_id"),
}
SOCIAL_SOURCES = {
    "instagram",
    "facebook",
    "snapchat",
    "linkedin",
    "venmo",
    "partiful",
    "spotify",
    "strava",
}


def link(record_id: str, owner_kind: str, owner_id: str, ops) -> None:
    """Keep a reviewed account. Inserting its relation before status is retryable."""
    table, column = OWNERS[owner_kind]
    records = somadata.sql(
        f"SELECT * FROM people_sync_records WHERE id = {somadata.sq(record_id)} "
        "AND deleted_at IS NULL"
    )
    if not records:
        sys.exit("no live contact record")
    record = records[0]
    if record["status"] not in ("pending", "ignored", "public") or record.get("person_id"):
        sys.exit("record belongs to personal triage; unlink it explicitly first")
    if record["source"] not in SOCIAL_SOURCES:
        sys.exit("only social accounts can be classified public")
    owners = somadata.sql(
        f"SELECT id FROM {table} WHERE id = {somadata.sq(owner_id)} AND deleted_at IS NULL"
    )
    if not owners:
        sys.exit("no live public owner")
    accounts = somadata.sql(
        "SELECT * FROM public_accounts "
        f"WHERE record_id = {somadata.sq(record_id)} OR id = {somadata.sq(record_id)}"
    )
    if accounts:
        if len(accounts) != 1:
            sys.exit("multiple public account relations; resolve explicitly")
        account = accounts[0]
        if account.get("deleted_at"):
            sys.exit("public account is deleted; restore explicitly")
        if (
            account["record_id"] != record_id
            or account.get(column) != owner_id
            or any(account.get(other) for _, other in OWNERS.values() if other != column)
        ):
            sys.exit("public account already has another owner; reassign explicitly")
    else:
        ops.insert(
            "public_accounts",
            [
                {
                    "id": record_id,
                    "record_id": record_id,
                    column: owner_id,
                }
            ],
        )
    ops.sql(
        "UPDATE people_sync_records SET status = 'public', suggested_person_id = NULL "
        f"WHERE id = {somadata.sq(record_id)} AND deleted_at IS NULL "
        "AND person_id IS NULL AND status IN ('pending', 'ignored', 'public')"
    )
