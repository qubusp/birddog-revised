-- Repair double-encoded names in core.location.
--
-- The NocoDB import landed some names as UTF-8 bytes that had been read as
-- Windows-1252 and encoded again: "Korsunâ€™-Shevchenkivsâ€™kyy" for
-- "Korsun’-Shevchenkivs’kyy", "Ã“sÃ¡ndorfalva" for "Ósándorfalva". On
-- 2026-11-25 that was 910 of 1201 rows (alternate_names 720, modern name 44,
-- c1900/c1930 district and province 203-264 each; c1950/c2000, region and
-- jewish_population were clean). raw.nocodb_record still holds the broken
-- text, so both transforms that copy it into core.location
-- (birddog/nocodb_import.sql, dags/sources/nocodb_locations/transform.sql)
-- call core.repair_mojibake too.
--
-- core.repair_mojibake(v) undoes the double encoding only when it is safe:
-- every character must map back to one Windows-1252 byte (the five bytes
-- Windows-1252 leaves undefined, 0x81 0x8D 0x8F 0x90 0x9D, came through as
-- the C1 controls U+0081... and map back to themselves), the bytes must be
-- valid UTF-8, and the result must hold no C1 control. Otherwise the value
-- is returned unchanged: a correctly encoded "Zürich" or "Łuck" fails one of
-- these tests. A value that mixes correct and broken text (a curator fixed
-- one spelling by hand) is left alone as well. It repeats while the value
-- changes (at most 3 times) so a triple-encoded value comes out clean and a
-- second run finds nothing to do.
--
-- Idempotent: re-running changes 0 rows.

CREATE OR REPLACE FUNCTION core.repair_mojibake(v text) RETURNS text
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    cur   text := v;
    bytes bytea;
    fixed text;
BEGIN
    FOR i IN 1..3 LOOP
        IF cur IS NULL OR cur !~ '[^\x01-\x7f]' THEN
            RETURN cur;
        END IF;
        BEGIN
            SELECT string_agg(CASE WHEN ascii(ch) IN (129, 141, 143, 144, 157)
                                   THEN decode(to_hex(ascii(ch)), 'hex')
                                   ELSE convert_to(ch, 'WIN1252') END,
                              ''::bytea ORDER BY n)
              INTO bytes
              FROM regexp_split_to_table(cur, '') WITH ORDINALITY AS t(ch, n);
            fixed := convert_from(bytes, 'UTF8');
        EXCEPTION WHEN others THEN
            RETURN cur;  -- not Windows-1252, or not valid UTF-8 underneath
        END;
        IF fixed = cur OR fixed ~ '[\u0080-\u009f]' THEN
            RETURN cur;
        END IF;
        cur := fixed;
    END LOOP;
    RETURN cur;
END $$;

COMMENT ON FUNCTION core.repair_mojibake(text) IS
    'Undo UTF-8 read as Windows-1252 and re-encoded, where that round-trips cleanly; otherwise return the value unchanged (migration 022).';

GRANT EXECUTE ON FUNCTION core.repair_mojibake(text) TO PUBLIC;

UPDATE core.location SET
    modern_location_name = core.repair_mojibake(modern_location_name),
    alternate_names      = core.repair_mojibake(alternate_names),
    c1900_district       = core.repair_mojibake(c1900_district),
    c1900_province       = core.repair_mojibake(c1900_province),
    c1930_district       = core.repair_mojibake(c1930_district),
    c1930_province       = core.repair_mojibake(c1930_province),
    c1950_district       = core.repair_mojibake(c1950_district),
    c1950_province       = core.repair_mojibake(c1950_province),
    c2000_district       = core.repair_mojibake(c2000_district),
    c2000_province       = core.repair_mojibake(c2000_province)
WHERE (modern_location_name, alternate_names, c1900_district, c1900_province,
       c1930_district, c1930_province, c1950_district, c1950_province,
       c2000_district, c2000_province)
      IS DISTINCT FROM
      (core.repair_mojibake(modern_location_name), core.repair_mojibake(alternate_names),
       core.repair_mojibake(c1900_district), core.repair_mojibake(c1900_province),
       core.repair_mojibake(c1930_district), core.repair_mojibake(c1930_province),
       core.repair_mojibake(c1950_district), core.repair_mojibake(c1950_province),
       core.repair_mojibake(c2000_district), core.repair_mojibake(c2000_province));
