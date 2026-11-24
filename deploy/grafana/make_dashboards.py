"""Writes deploy/grafana/dashboards/*.json, the provisioned Grafana dashboards.

The JSON files are what Grafana loads; this script is how they are made, so
panels stay consistent. Edit here, run `python deploy/grafana/make_dashboards.py`
and commit both. Every query reads the birddog datasource (uid "birddog") as
birddog_reader, so a new view needs a GRANT to that role.
"""
import json
import pathlib

OUT = pathlib.Path(__file__).resolve().parent / "dashboards"
DS = {"type": "grafana-postgresql-datasource", "uid": "birddog"}
UKRAINE_VIEW = {"id": "coords", "lat": 49.0, "lon": 30.5, "zoom": 5.4}


def target(sql, fmt="table", ref="A"):
    return {"refId": ref, "datasource": DS, "editorMode": "code", "format": fmt,
            "rawQuery": True, "rawSql": sql.strip()}


def stat(pid, title, sql, x, y, w=4, h=4, unit="short", decimals=None):
    p = {"id": pid, "type": "stat", "title": title, "datasource": DS,
         "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": [target(sql)],
         "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                     "colorMode": "none", "graphMode": "none", "textMode": "value"},
         "fieldConfig": {"defaults": {"unit": unit}, "overrides": []}}
    if decimals is not None:
        p["fieldConfig"]["defaults"]["decimals"] = decimals
    return p


def table(pid, title, sql, x, y, w, h, links=None):
    p = {"id": pid, "type": "table", "title": title, "datasource": DS,
         "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": [target(sql)],
         "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
         "fieldConfig": {"defaults": {}, "overrides": []}}
    for field, url in (links or {}).items():
        p["fieldConfig"]["overrides"].append({
            "matcher": {"id": "byName", "options": field},
            "properties": [{"id": "links", "value": [{"title": "Open", "url": url, "targetBlank": True}]}]})
    return p


def bars(pid, title, sql, x, y, w, h, horizontal=True):
    return {"id": pid, "type": "barchart", "title": title, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": [target(sql)],
            "options": {"orientation": "horizontal" if horizontal else "vertical",
                        "showValue": "auto", "legend": {"showLegend": False},
                        "xTickLabelRotation": 0, "barWidth": 0.8},
            "fieldConfig": {"defaults": {"color": {"mode": "fixed", "fixedColor": "#1f5f57"}}, "overrides": []}}


def pie(pid, title, sql, x, y, w, h):
    return {"id": pid, "type": "piechart", "title": title, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": [target(sql)],
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": True},
                        "pieType": "donut", "legend": {"displayMode": "table", "placement": "right",
                                                       "values": ["value"]}},
            "fieldConfig": {"defaults": {}, "overrides": []}}


def timeseries(pid, title, sql, x, y, w, h, bars_style=True):
    return {"id": pid, "type": "timeseries", "title": title, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": [target(sql, fmt="time_series")],
            "options": {"legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
                        "tooltip": {"mode": "multi"}},
            "fieldConfig": {"defaults": {"custom": {"drawStyle": "bars" if bars_style else "line",
                                                    "fillOpacity": 70, "lineWidth": 1}},
                            "overrides": []}}


def geomap(pid, title, sql, x, y, w, h, size_field, color_field=None, layer_name="Places"):
    color = ({"field": color_field, "fixed": "#1f5f57"} if color_field
             else {"fixed": "#1f5f57"})
    return {"id": pid, "type": "geomap", "title": title, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": [target(sql)],
            "options": {
                "view": UKRAINE_VIEW,
                "controls": {"showZoom": True, "mouseWheelZoom": True, "showAttribution": True,
                             "showScale": True},
                "tooltip": {"mode": "details"},
                "basemap": {"type": "osm-standard", "name": "OpenStreetMap"},
                "layers": [{
                    "type": "markers", "name": layer_name, "tooltip": True,
                    "location": {"mode": "coords", "latitude": "latitude", "longitude": "longitude"},
                    "config": {"showLegend": True, "style": {
                        "size": {"field": size_field, "fixed": 4, "min": 3, "max": 22},
                        "color": color, "opacity": 0.7,
                        "symbol": {"mode": "fixed", "fixed": "img/icons/marker/circle.svg"}}}}]},
            "fieldConfig": {"defaults": {"color": {"mode": "continuous-GrYlRd"}}, "overrides": []}}


def dashboard(uid, title, description, panels, time_from="now-30d", tags=("birddog",)):
    return {"uid": uid, "title": title, "description": description, "tags": list(tags),
            "editable": True, "graphTooltip": 1, "schemaVersion": 41, "version": 1,
            "time": {"from": time_from, "to": "now"}, "timezone": "utc", "refresh": "",
            "panels": panels, "templating": {"list": []}, "annotations": {"list": []},
            "links": [{"title": "Other dashboards", "type": "dashboards", "tags": ["birddog"],
                       "asDropdown": True}]}


# --- Archive map ------------------------------------------------------------------
archive = dashboard("archive-map", "Archive map",
    "Every location in the catalog, from the NocoDB coordinates (migration 021). "
    "Size: how often the books name the place. Document counts fill in once documents are linked to places.",
    [
        stat(1, "Locations", "SELECT count(*) FROM views.map__locations", 0, 0),
        stat(2, "Named in books", "SELECT count(*) FROM views.map__locations WHERE books > 0", 4, 0),
        stat(3, "Regions", "SELECT count(DISTINCT region) FROM views.map__locations", 8, 0),
        stat(4, "Linked documents", "SELECT coalesce(sum(documents), 0) FROM views.map__locations", 12, 0),
        geomap(5, "Locations (size: mentions in books)", """
SELECT name, region, c1900_province AS "province (1900)", c1900_district AS "district (1900)",
       jewish_population AS "Jewish population", alternate_names AS "other names",
       books, book_mentions, documents, latitude, longitude
FROM views.map__locations
ORDER BY book_mentions""", 0, 4, 16, 18, size_field="book_mentions", color_field="book_mentions"),
        bars(6, "Locations per region", """
SELECT coalesce(region, '(none)') AS region, count(*) AS locations
FROM views.map__locations GROUP BY 1 ORDER BY 2 DESC""", 16, 0, 8, 22),
        table(7, "Locations", """
SELECT name, region, c1900_province AS "province (1900)", c1900_district AS "district (1900)",
       jewish_population AS "Jewish population", books, book_mentions AS "mentions in books", documents
FROM views.map__locations ORDER BY book_mentions DESC, name""", 0, 22, 24, 12),
    ])

# --- Books map --------------------------------------------------------------------
books_map = dashboard("books-map", "Books map",
    "Places Claude read in the archive books (read_books), matched to catalog locations. "
    "Size: mentions. Click a book in the table to open its scan.",
    [
        stat(1, "Books read", "SELECT count(*) FROM views.stats__books WHERE status = 'done'", 0, 0),
        stat(2, "Places found", "SELECT count(*) FROM views.books__places", 4, 0),
        stat(3, "Matched to a location", "SELECT count(*) FROM views.map__book_places", 8, 0),
        stat(4, "Mentions on pages", "SELECT count(*) FROM views.books__place_mentions", 12, 0),
        stat(5, "Distinct locations", "SELECT count(DISTINCT location_id) FROM views.map__book_places", 16, 0),
        stat(6, "Match rate", """
SELECT round(100.0 * (SELECT count(*) FROM views.map__book_places)
       / nullif((SELECT count(*) FROM views.books__places), 0), 1)""", 20, 0, unit="percent", decimals=1),
        geomap(7, "Places named in books (size: mentions)", """
SELECT location AS place, name_uk AS "as read", region, book, mentions, pages, kinds,
       admin_context AS "context in book", latitude, longitude
FROM views.map__book_places ORDER BY mentions""", 0, 4, 16, 18, size_field="mentions", color_field="mentions",
               layer_name="Book places"),
        bars(8, "Most named places", """
SELECT location AS place, sum(mentions) AS mentions
FROM views.map__book_places GROUP BY 1 ORDER BY 2 DESC LIMIT 25""", 16, 4, 8, 18),
        table(9, "Places per book", """
SELECT book, name_uk AS "as read", name_en, location, region, mentions, pages, match_score, url
FROM views.map__book_places ORDER BY book, mentions DESC""", 0, 22, 24, 12,
              links={"book": "${__data.fields.url}"}),
        table(10, "Unmatched places (no location yet)", """
SELECT title AS book, name_uk AS "as read", name_en, admin_context AS context, mentions, pages, kinds
FROM views.books__places WHERE location_id IS NULL ORDER BY mentions DESC""", 0, 34, 24, 10),
    ])

# --- Collection overview ----------------------------------------------------------
collection = dashboard("collection", "Collection overview",
    "The catalog: archive pages and documents tracked on uk.wikisource.org, translations and FTP matching. "
    "Time panels use the wiki timestamps (when a scan or page last changed on the wiki); widen the time range.",
    [
        stat(1, "Archive pages", "SELECT count(*) FROM core.page", 0, 0),
        stat(2, "Documents", "SELECT count(*) FROM core.document", 4, 0),
        stat(3, "Scan pages", "SELECT coalesce(sum(page_count), 0) FROM core.document", 8, 0),
        stat(4, "Translations", "SELECT count(*) FROM core.translation", 12, 0),
        stat(5, "FTP files", "SELECT count(*) FROM core.ftp_entry", 16, 0),
        stat(6, "FTP files matched", "SELECT count(*) FROM core.ftp_document", 20, 0),
        timeseries(7, "Documents by wiki upload month", """
SELECT date_trunc('month', wiki_timestamp) AS time, count(*) AS documents
FROM core.document
WHERE wiki_timestamp IS NOT NULL AND $__timeFilter(wiki_timestamp)
GROUP BY 1 ORDER BY 1""", 0, 4, 12, 9),
        timeseries(8, "Archive pages changed on the wiki, by month", """
SELECT date_trunc('month', wiki_timestamp) AS time, count(*) AS pages
FROM core.page
WHERE wiki_timestamp IS NOT NULL AND $__timeFilter(wiki_timestamp)
GROUP BY 1 ORDER BY 1""", 12, 4, 12, 9),
        bars(9, "Documents per archive", """
SELECT coalesce(p.root_label, '(no page)') AS archive, count(DISTINCT pd.document_id) AS documents
FROM core.page_document pd JOIN core.page p ON p.id = pd.page_id
GROUP BY 1 ORDER BY 2 DESC LIMIT 20""", 0, 13, 12, 12),
        pie(10, "Documents by processing code", """
SELECT coalesce(nullif(process_code, ''), '(none)') AS code, count(*) AS documents
FROM core.document GROUP BY 1 ORDER BY 2 DESC""", 12, 13, 12, 12),
        pie(11, "Documents by availability", """
SELECT coalesce(availability, '(unknown)') AS availability, count(*) AS documents
FROM core.document GROUP BY 1 ORDER BY 2 DESC""", 0, 25, 8, 9),
        pie(12, "Translations by target language", """
SELECT coalesce(target_lang, '(unknown)') AS language, count(*) AS translations
FROM core.translation GROUP BY 1 ORDER BY 2 DESC""", 8, 25, 8, 9),
        table(13, "Work queues", """
SELECT 'Documents without location' AS queue, count(*) AS items FROM core.v_docs_no_location
UNION ALL SELECT 'Documents without metadata', count(*) FROM core.v_docs_no_meta
UNION ALL SELECT 'Documents needing duplicate check', count(*) FROM core.v_docs_need_dupe_check
UNION ALL SELECT 'FTP files to match', count(*) FROM core.v_ftp_need_match
UNION ALL SELECT 'Stale archive pages', count(*) FROM core.v_pages_stale
UNION ALL SELECT 'Untranslated pages', count(*) FROM core.v_untranslated""", 16, 25, 8, 9),
    ], time_from="now-10y")

# --- Book reading costs -----------------------------------------------------------
costs = dashboard("books-costs", "Book reading: progress and costs",
    "read_books (birddog/books.py): status, pages read, tokens and dollars, per book and per model.",
    [
        stat(1, "Spent", "SELECT coalesce(sum(cost_usd), 0) FROM views.stats__books", 0, 0, unit="currencyUSD", decimals=2),
        stat(2, "Estimated (queued books)", "SELECT coalesce(sum(est_cost_usd), 0) FROM views.stats__books WHERE status NOT IN ('done', 'rejected')", 4, 0, unit="currencyUSD", decimals=2),
        stat(3, "Pages read", "SELECT count(*) FROM core.book_page", 8, 0),
        stat(4, "Input tokens", "SELECT coalesce(sum(input_tokens), 0) FROM views.stats__books", 12, 0),
        stat(5, "Output tokens", "SELECT coalesce(sum(output_tokens), 0) FROM views.stats__books", 16, 0),
        stat(6, "Cost per page", """
SELECT coalesce(sum(cost_usd), 0) / nullif((SELECT count(*) FROM core.book_page), 0) FROM views.stats__books""",
             20, 0, unit="currencyUSD", decimals=3),
        pie(7, "Books by status", "SELECT status, count(*) AS books FROM views.stats__books GROUP BY 1 ORDER BY 2 DESC",
            0, 4, 8, 9),
        bars(8, "Cost by model", """
SELECT model, round(sum(cost_usd)::numeric, 2) AS dollars FROM core.book_page GROUP BY 1 ORDER BY 2 DESC""",
             8, 4, 8, 9),
        timeseries(9, "Spend per day", """
SELECT date_trunc('day', created_at) AS time, sum(cost_usd) AS dollars
FROM core.book_page WHERE $__timeFilter(created_at) GROUP BY 1 ORDER BY 1""", 16, 4, 8, 9),
        table(10, "Books", """
SELECT title, status, page_count AS pages, pages_read, places, round(cost_usd::numeric, 2) AS "cost $",
       round(est_cost_usd::numeric, 2) AS "estimate $", input_tokens, output_tokens, updated_at
FROM views.stats__books ORDER BY updated_at DESC""", 0, 13, 24, 12),
    ])

for name, dash in (("archive-map", archive), ("books-map", books_map), ("collection", collection),
                   ("books-costs", costs)):
    (OUT / f"{name}.json").write_text(json.dumps(dash, indent=2, ensure_ascii=False) + "\n")
    print("wrote", name, len(dash["panels"]), "panels")
