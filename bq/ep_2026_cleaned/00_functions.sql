-- ep_2026_cleaned identity-normalization UDFs.
--
-- The define-once identity contract for the 2026 EP interface layer: every
-- view in ep_2026_cleaned normalizes email/phone through these functions, so
-- consumers never re-normalize (and never join raw values across sources).
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
