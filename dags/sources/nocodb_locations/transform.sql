-- nocodb_locations: landed NocoDB Locations -> core.location.
-- `landed` holds only the records that are new or changed since the last
-- transform (key = location_id, payload = the NocoDB fields), so unchanged
-- locations - and any curator edits to them in core - are left alone.
-- Same column mapping as birddog/nocodb_import.sql, including the repair of
-- double-encoded names (core.repair_mojibake, migration 022).
INSERT INTO core.location (location_id, modern_location_name, alternate_names,
    c1900_district, c1900_province, c1930_district, c1930_province,
    c1950_district, c1950_province, c2000_district, c2000_province)
SELECT key,
    core.repair_mojibake(payload->>'modern_location_name'), core.repair_mojibake(payload->>'alternate_names'),
    core.repair_mojibake(payload->>'c1900_district'), core.repair_mojibake(payload->>'c1900_province'),
    core.repair_mojibake(payload->>'c1930_district'), core.repair_mojibake(payload->>'c1930_province'),
    core.repair_mojibake(payload->>'c1950_district'), core.repair_mojibake(payload->>'c1950_province'),
    core.repair_mojibake(payload->>'c2000_district'), core.repair_mojibake(payload->>'c2000_province')
FROM landed
ON CONFLICT (location_id) DO UPDATE SET
    modern_location_name = EXCLUDED.modern_location_name, alternate_names = EXCLUDED.alternate_names,
    c1900_district = EXCLUDED.c1900_district, c1900_province = EXCLUDED.c1900_province,
    c1930_district = EXCLUDED.c1930_district, c1930_province = EXCLUDED.c1930_province,
    c1950_district = EXCLUDED.c1950_district, c1950_province = EXCLUDED.c1950_province,
    c2000_district = EXCLUDED.c2000_district, c2000_province = EXCLUDED.c2000_province;

-- Locations deleted in NocoDB (rows of `gone`) are kept in core: documents may
-- link to them, and deleting is a curator decision. They show in
-- views.source__nocodb_locations with in_source = false.
