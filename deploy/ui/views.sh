#!/bin/bash
# Adds "Editable version" links to the curator views (db/migrations/006_curator_views.sql).
# Idempotent; ui/setup.sh runs it after the migrations. Run from deploy/:  bash ui/views.sh
#
# Mathesar 0.12 shows Postgres views read-only (it edits rows by primary key,
# and views have none). It has no saved filters on tables either, but a table
# page keeps its filter / sort / hidden columns in the URL (?q=, base64url of
# [pagination, sorting, grouping, filtering, joining, hiddenColumns]). For
# each curator view whose filter only uses its core table's own columns, this
# script builds that URL (the editable twin) and appends it to the view's
# COMMENT, which Mathesar shows as the view's description. Views that filter
# on link counts or relative dates have no editable twin.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

db_id=$(docker compose exec -T mathesar python manage.py shell -c \
    "from mathesar.models.base import Database; print(Database.objects.get(name='birddog').id)" \
    2>/dev/null | tail -1)
[[ "$db_id" =~ ^[0-9]+$ ]] || { echo "birddog database not registered in Mathesar"; exit 1; }

docker compose exec -T -e PGPASSWORD="$BIRDDOG_DB_PASSWORD" postgres \
    psql -h localhost -U birddog -d birddog -v ON_ERROR_STOP=1 -q \
    -v base="https://data.${PUBLIC_DOMAIN}/db/${db_id}" <<'SQL'
SELECT set_config('ui.base', :'base', false) AS ignored \gset

-- filter terse JSON with column names -> with attnums (recursing into groups)
CREATE FUNCTION pg_temp.resolve(f jsonb, att jsonb) RETURNS jsonb LANGUAGE plpgsql AS $$
BEGIN
    RETURN (SELECT coalesce(jsonb_agg(
                CASE WHEN e->>0 = 'i'
                     THEN jsonb_build_array('i', att->>(e->>1), e->>2, e->3)
                     ELSE jsonb_build_array('g', e->>1, pg_temp.resolve(e->2, att)) END
                ORDER BY o), '[]')
              FROM jsonb_array_elements(f) WITH ORDINALITY x(e, o));
END $$;

-- one row per editable twin: view, core table, filter and sort (Mathesar's terse
-- JSON, with column names that are resolved to attnums below). Hidden columns:
-- every table column the view doesn't select.
CREATE TEMP TABLE twin (
    view_name text, table_name text, filter jsonb, sort jsonb
);
INSERT INTO twin VALUES
 ('pages__all', 'page', NULL, '[["id","ASCENDING"]]'),
 ('pages__cdiak', 'page', '[["i","root_label","equal","CDIAK"]]', '[["seq_label","ASCENDING"]]'),
 ('pages__juliana_test', 'page',
  '[["i","root_label","equal","DACHVO"],
    ["g","or",[["i","level","equal","archive"],["i","level","equal","fond"],["i","level","equal","opus"]]]]',
  '[["label","ASCENDING"]]'),
 ('pages__import_messages', 'page', '[["i","import_message","not_null",null]]',
  '[["last_imported","DESCENDING"]]'),
 ('documents__all', 'document', NULL, '[["id","ASCENDING"]]'),
 ('documents__cdiak', 'document', NULL, '[["id","ASCENDING"]]'),
 ('locations__locations', 'location', NULL, '[["location_id","ASCENDING"]]');

DO $$
DECLARE
    t record; tab regclass; att jsonb; terse jsonb; hidden jsonb; url text; base_comment text;
BEGIN
    FOR t IN SELECT * FROM twin LOOP
        tab := format('core.%I', t.table_name)::regclass;
        -- column name -> attnum (as a string, like Mathesar's columnId)
        SELECT jsonb_object_agg(attname, attnum::text) INTO att
          FROM pg_attribute WHERE attrelid = tab AND attnum > 0 AND NOT attisdropped;
        -- hide the table columns the NocoDB view didn't show (those not in the curator view)
        SELECT coalesce(jsonb_agg(a.attnum::text ORDER BY a.attnum), '[]') INTO hidden
          FROM pg_attribute a
         WHERE a.attrelid = tab AND a.attnum > 0 AND NOT a.attisdropped
           AND a.attname NOT IN (SELECT c.column_name FROM information_schema.columns c
                                  WHERE c.table_schema = 'views' AND c.table_name = t.view_name);
        terse := jsonb_build_array(
            jsonb_build_array(1, 500),                                    -- pagination
            (SELECT coalesce(jsonb_agg(jsonb_build_array(att->>(s->>0), s->>1)), '[]')
               FROM jsonb_array_elements(t.sort) s),                      -- sorting
            '[]'::jsonb,                                                  -- grouping
            jsonb_build_array('g', 'and', pg_temp.resolve(coalesce(t.filter, '[]'), att)),
            '[]'::jsonb,                                                  -- joining
            hidden);
        url := current_setting('ui.base')
            || format('/schemas/%s/tables/%s/?q=', 'core'::regnamespace::oid, tab::oid)
            || rtrim(translate(encode(convert_to(terse::text, 'UTF8'), 'base64'),
                               E'+/\n', '-_'), '=');
        base_comment := regexp_replace(coalesce(obj_description(format('views.%I', t.view_name)::regclass, 'pg_class'), ''),
                                       '\s*Editable version.*$', '');
        EXECUTE format('COMMENT ON VIEW views.%I IS %L', t.view_name,
                       base_comment || E'\nEditable version (same filter on core.' || t.table_name || '): ' || url);
        RAISE NOTICE '%: %', t.view_name, url;
    END LOOP;
END $$;
SQL
