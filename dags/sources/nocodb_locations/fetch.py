"""Fetch for nocodb_locations: every record of one NocoDB table, through the
paced client in birddog.nocodb_import (it backs off on NocoDB Cloud's 429s).

Yields each record's fields plus its NocoDB id as `nocodb_id`."""

import requests

from birddog import nocodb_import as nc


def fetch(ctx):
    host, base, token = nc._config()
    session = requests.Session()
    session.headers["xc-token"] = token
    pacer = nc._Pacer()
    table_id = nc._table_ids(session, host, base)[ctx.options.get("table", "Locations")]
    url = f"{host}/api/v3/data/{base}/{table_id}/records"

    last_id, skipped = 0, 0
    while True:
        data = nc._get(session, url, {"where": f"(Id,gt,{last_id})", "pageSize": nc._PAGE_SIZE},
                       pacer=pacer)
        records = data.get("records", [])
        if not records:
            break
        for r in records:
            fields = dict(r["fields"])
            if not (fields.get("location_id") or "").strip():
                skipped += 1
                continue
            yield {"nocodb_id": r["id"], **fields}
        new_last = max(r["id"] for r in records)
        if new_last <= last_id:
            raise RuntimeError(f"paging stalled at id {last_id}")
        last_id = new_last
        if len(records) < nc._PAGE_SIZE:
            break
    if skipped:
        ctx.log.warning(f"{ctx.name}: skipped {skipped} records without a location_id")
