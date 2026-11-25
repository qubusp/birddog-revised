-- Coordinates for locations, and views for Grafana's maps and dashboards
-- (deploy/grafana/).
--
-- NocoDB's Locations table had latitude, longitude, region and
-- jewish_population, but import_nocodb's transform only kept the names and
-- districts. They are copied here from raw.nocodb_record (all 1201 locations
-- have coordinates). jewish_population stays text: it reads like
-- "2,154 (in 1897), 1,293 (in 1921)".
--
--   views.map__locations      one row per location with coordinates: names,
--                             region, 1900 district/province, population note,
--                             documents linked, and how often the books name it
--   views.map__book_places    one row per place a book names that is matched to
--                             a location: book, place, mentions, coordinates
--   views.stats__books        read_books per book: status, pages, places,
--                             tokens and cost
--
-- Read by Grafana as birddog_reader. Idempotent.

ALTER TABLE core.location ADD COLUMN IF NOT EXISTS latitude double precision;
ALTER TABLE core.location ADD COLUMN IF NOT EXISTS longitude double precision;
ALTER TABLE core.location ADD COLUMN IF NOT EXISTS region text;
ALTER TABLE core.location ADD COLUMN IF NOT EXISTS jewish_population text;

UPDATE core.location l
SET latitude          = (r.fields ->> 'latitude')::double precision,
    longitude         = (r.fields ->> 'longitude')::double precision,
    region            = nullif(r.fields ->> 'region', ''),
    jewish_population = nullif(r.fields ->> 'jewish_population', '')
FROM raw.nocodb_record r
WHERE r.table_name = 'locations'
  AND r.fields ->> 'location_id' = l.location_id
  AND jsonb_typeof(r.fields -> 'latitude') = 'number'
  AND jsonb_typeof(r.fields -> 'longitude') = 'number'
  AND (l.latitude IS DISTINCT FROM (r.fields ->> 'latitude')::double precision
       OR l.longitude IS DISTINCT FROM (r.fields ->> 'longitude')::double precision
       OR l.region IS DISTINCT FROM nullif(r.fields ->> 'region', '')
       OR l.jewish_population IS DISTINCT FROM nullif(r.fields ->> 'jewish_population', ''));

DROP VIEW IF EXISTS views.map__locations;
CREATE VIEW views.map__locations AS
SELECT l.location_id,
       l.modern_location_name                    AS name,
       l.alternate_names,
       l.region,
       l.c1900_district,
       l.c1900_province,
       l.jewish_population,
       l.latitude,
       l.longitude,
       (SELECT count(*) FROM core.document_location dl
         WHERE dl.location_id = l.location_id)    AS documents,
       coalesce(bp.books, 0)                      AS books,
       coalesce(bp.mentions, 0)                   AS book_mentions
FROM core.location l
LEFT JOIN (
    SELECT location_id, count(DISTINCT document_id) AS books, sum(mentions) AS mentions
    FROM views.books__places
    WHERE location_id IS NOT NULL
    GROUP BY location_id
) bp ON bp.location_id = l.location_id
WHERE l.latitude IS NOT NULL AND l.longitude IS NOT NULL;
COMMENT ON VIEW views.map__locations IS
    'Grafana: every location with coordinates, with linked documents and how often the books name it.';

DROP VIEW IF EXISTS views.map__book_places;
CREATE VIEW views.map__book_places AS
SELECT bp.document_id,
       bp.title                                   AS book,
       bp.name_uk,
       bp.name_en,
       bp.mentions,
       bp.pages,
       bp.kinds,
       bp.admin_context,
       bp.match_score,
       l.location_id,
       l.modern_location_name                     AS location,
       l.region,
       l.latitude,
       l.longitude,
       bp.url
FROM views.books__places bp
JOIN core.location l ON l.location_id = bp.location_id
WHERE l.latitude IS NOT NULL;
COMMENT ON VIEW views.map__book_places IS
    'Grafana: places named in books that are matched to a location, with coordinates.';

DROP VIEW IF EXISTS views.stats__books;
CREATE VIEW views.stats__books AS
SELECT b.document_id,
       d.title,
       b.status,
       b.page_count,
       (SELECT count(*) FROM core.book_page p WHERE p.document_id = b.document_id) AS pages_read,
       jsonb_array_length(coalesce(b.places -> 'places', '[]'::jsonb))             AS places,
       b.input_tokens,
       b.output_tokens,
       b.cost_usd,
       b.est_cost_usd,
       b.queued_at,
       b.updated_at
FROM core.book_read b
JOIN core.document d ON d.id = b.document_id;
COMMENT ON VIEW views.stats__books IS
    'Grafana: read_books per book: status, pages read, places found, tokens and cost.';

DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_reader') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA views TO birddog_reader';
        EXECUTE 'GRANT SELECT ON views.map__locations, views.map__book_places, views.stats__books, views.books__places, views.books__place_mentions TO birddog_reader';
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'birddog_ui') THEN
        EXECUTE 'GRANT SELECT ON views.map__locations, views.map__book_places, views.stats__books TO birddog_ui';
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'marmot_reader') THEN
        EXECUTE 'GRANT SELECT ON views.map__locations, views.map__book_places, views.stats__books TO marmot_reader';
    END IF;
END $$;
