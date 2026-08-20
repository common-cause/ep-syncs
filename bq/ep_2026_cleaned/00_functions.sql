-- ep_2026_cleaned identity-normalization UDFs.
--
-- The define-once identity contract for the 2026 EP interface layer: every
-- view in ep_2026_cleaned normalizes email/phone/state through these
-- functions, so consumers never re-normalize (and never join raw values
-- across sources).
--
-- Apply with: python apply_bq_views.py --only 00_functions.sql
-- (or let a full apply run pick it up first, by filename order).

CREATE OR REPLACE FUNCTION `proj-tmc-mem-com.ep_2026_cleaned.norm_email`(e STRING)
RETURNS STRING
OPTIONS(description="Identity email normalization for the 2026 EP interface layer: TRIM + LOWER, blank -> NULL. Every ep_2026_cleaned view exposes email through this; join views on the normalized value.")
AS (
  NULLIF(TRIM(LOWER(e)), '')
);

CREATE OR REPLACE FUNCTION `proj-tmc-mem-com.ep_2026_cleaned.norm_phone`(p STRING)
RETURNS STRING
OPTIONS(description="Phone normalization for the 2026 EP interface layer: strip non-digits, keep the last 10 (drops US country code), blank -> NULL. NOTE: unlike the legacy ptv_raw_2026 views' RIGHT(phone, 10), this strips punctuation first.")
AS (
  NULLIF(RIGHT(REGEXP_REPLACE(COALESCE(p, ''), r'[^0-9]', ''), 10), '')
);

-- Why a UDF and not a dim table: every ep_2026_cleaned view already keys on the
-- USPS code, and the sources that DON'T (hand-maintained sheets, state-run
-- tools) each arrive with their own spelling of the same 51 places. A function
-- keeps the mapping in one auditable place with no join and no seed table to
-- keep in sync, and the alias list below is the honest record of the specific
-- ways our sources are wrong. Add an alias row when a new source misspells
-- something -- do NOT fuzzy-match; a wrong state is worse than a NULL one.
CREATE OR REPLACE FUNCTION `proj-tmc-mem-com.ep_2026_cleaned.norm_state`(s STRING)
RETURNS STRING
OPTIONS(description="State normalization for the 2026 EP interface layer: accepts a USPS 2-letter code OR a full state name (any case, tolerating NBSP/zero-width characters, doubled spaces and trailing periods/commas) and returns the 2-letter code; unrecognized input -> NULL. Covers 50 states + DC. Known source aliases are mapped explicitly: 'Lousiana' (the coalition plan's row-22 misspelling), 'District of Columbia' and 'Washington DC'. Join EP sources on this value, never on a raw state label.")
AS (
  (
    SELECT m.code
    FROM UNNEST([
      -- Normalize once: invisible characters (pasted text carries NBSP,
      -- narrow-NBSP, zero-width space and BOM) -> space, drop sentence
      -- punctuation so 'D.C.' and 'Ohio.' resolve, collapse runs of space.
      TRIM(REGEXP_REPLACE(
        REGEXP_REPLACE(
          UPPER(REGEXP_REPLACE(COALESCE(s, ''),
                               r'[\x{00a0}\x{202f}\x{200b}\x{feff}]', ' ')),
          r'[.,]', ''),
        r'\s+', ' '))
    ]) AS k
    CROSS JOIN UNNEST([
      STRUCT('AL' AS code, 'ALABAMA' AS label),
      ('AK', 'ALASKA'),
      ('AZ', 'ARIZONA'),
      ('AR', 'ARKANSAS'),
      ('CA', 'CALIFORNIA'),
      ('CO', 'COLORADO'),
      ('CT', 'CONNECTICUT'),
      ('DE', 'DELAWARE'),
      ('DC', 'DISTRICT OF COLUMBIA'),
      ('DC', 'WASHINGTON DC'),          -- alias
      ('FL', 'FLORIDA'),
      ('GA', 'GEORGIA'),
      ('HI', 'HAWAII'),
      ('ID', 'IDAHO'),
      ('IL', 'ILLINOIS'),
      ('IN', 'INDIANA'),
      ('IA', 'IOWA'),
      ('KS', 'KANSAS'),
      ('KY', 'KENTUCKY'),
      ('LA', 'LOUISIANA'),
      ('LA', 'LOUSIANA'),               -- alias: coalition plan row 22
      ('ME', 'MAINE'),
      ('MD', 'MARYLAND'),
      ('MA', 'MASSACHUSETTS'),
      ('MI', 'MICHIGAN'),
      ('MN', 'MINNESOTA'),
      ('MS', 'MISSISSIPPI'),
      ('MO', 'MISSOURI'),
      ('MT', 'MONTANA'),
      ('NE', 'NEBRASKA'),
      ('NV', 'NEVADA'),
      ('NH', 'NEW HAMPSHIRE'),
      ('NJ', 'NEW JERSEY'),
      ('NM', 'NEW MEXICO'),
      ('NY', 'NEW YORK'),
      ('NC', 'NORTH CAROLINA'),
      ('ND', 'NORTH DAKOTA'),
      ('OH', 'OHIO'),
      ('OK', 'OKLAHOMA'),
      ('OR', 'OREGON'),
      ('PA', 'PENNSYLVANIA'),
      ('RI', 'RHODE ISLAND'),
      ('SC', 'SOUTH CAROLINA'),
      ('SD', 'SOUTH DAKOTA'),
      ('TN', 'TENNESSEE'),
      ('TX', 'TEXAS'),
      ('UT', 'UTAH'),
      ('VT', 'VERMONT'),
      ('VA', 'VIRGINIA'),
      ('WA', 'WASHINGTON'),
      ('WV', 'WEST VIRGINIA'),
      ('WI', 'WISCONSIN'),
      ('WY', 'WYOMING')
    ]) AS m
    WHERE k = m.code OR k = m.label
    LIMIT 1
  )
);
