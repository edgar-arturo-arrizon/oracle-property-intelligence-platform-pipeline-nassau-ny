# Town of Oyster Bay eTRAKiT permit harvester

Status (2026-10-25): **blocked before the pilot.** The harvester was not built beyond the session guard and the scope, because the one public login attempt returned "Invalid Login". Nothing was harvested and no background process was started.

## Login finding

- **Portal:** `https://oys-trk.aspgov.com/eTRAKiT/`. The public login form is on `login.aspx` (`cplMain_txtPublicUserName`, `cplMain_txtPublicPassword`, `cplMain_btnPublicLogin`). It has no captcha, challenge, or lockout wording (login page and failed-login page both checked).
- **One attempt only:** with the `ETRAKIT_USERNAME` / `ETRAKIT_PASSWORD` values in `.env`, the portal answered **"Invalid Login"**.
- **Likely cause:** the failed-login screenshot shows a generic template placeholder word in the username field (a 13-letter phrase of the "your login name" kind), and the `.env` value lengths (13 and 12 letters, letters only) are consistent with template placeholders rather than a real account. The operator should check `.env`. I did not retry (lockout risk), and I did not print or log any value.
- **Resume:** once `.env` holds the real public account, a pilot of 25 permits can run immediately.

## Safety design (implemented in `pipeline/etrakit_session.py`)

- Credentials are read only from `.env` (gitignored and verified: `.gitignore:12`). Every string that leaves the module goes through `scrub()`, which also covers the URL-encoded and HTML-escaped forms.
- One browser action at a time, spaced at least 1.1 s, with exponential backoff on transient errors. Images, fonts, and media are not fetched.
- Captcha, challenge, 403/429, or lockout wording raises `PortalBlocked`. The caller stops, records it in the docs and in `pipeline_runs.limitations`, and does not retry around it.

## Demo scope (derived offline, `pipeline/etrakit_scope.py`)

- **Population:** Town of Oyster Bay **unincorporated** parcels, SWIS `282489` (81,752 parcels). Incorporated villages (Bayville, Farmingdale, Massapequa Park, Sea Cliff, and others) issue their own permits and are not in this portal.
- **Centre and radius:** Hicksville (40.7684, -73.5251), 3 miles.
- **Coordinates:** NYS centroid lat/lon, else the GIS polygon's point-on-surface. 81,074 of 81,752 have one; the other 678 cannot be placed, and 34 of those sit in scoped blocks.
- **Rule:** every section-block containing at least one in-radius parcel is searched in full, so parcels just outside the circle in the same block are included.
- **Result:** **1,424 blocks, 32,573 parcels** (31,677 of them inside the circle), across 10 sections: 11 (210 blocks), 12 (540), 13 (9), 15 (70), 16 (1), 17 (5), 30 (7), 45 (170), 46 (363), 47 (49).
- **Stored in** `etrakit_scope_parcels` and `etrakit_scope_blocks` in `data/nassau.duckdb` (rebuild with `.venv/bin/python -m pipeline.etrakit_scope`).

## SBL mapping (print key to eTRAKiT search key)

- Our print key `46.-334-12` becomes section 46, block 334, lot 12. The block search key is `<section>-<block>-`, e.g. `46-334-`, the form the operator verified by hand.
- A block that hits the 100-result cap is split by lot prefix (`46-334-1` through `46-334-9`).
- **Not yet verified:** whether eTRAKiT zero-pads lots, and how it shows suffixes. In scope the print-key shapes are `S.-B-L` (30,843), `S.-A-L` alphabetic block (757), `S.-B-L.N/U` condo units (619), `S.-B-L.A` (268), and a few others. Block `B18`-style alphanumeric blocks also exist. A logged-in probe must confirm the format.

## Estimated harvest size (unverified, until the pilot measures it)

- Search requests: at least 1,424, plus up to 4 extra result pages per block (20 per page, 100 cap) and extra lot-prefix splits for capped blocks.
- Detail requests: unknown. The operator reports 23,751 records for the whole of section 46, which holds 363 of our blocks, so the scope may hold on the order of tens of thousands of permits. Each permit needs its detail page plus the Site Info and Contacts tabs, so about 3 requests per permit.
- At about 1 request/second, 30,000 permits would take about 25 hours and 90,000 permits about 75 hours. **If the pilot confirms more than 48 hours, I will stop and ask** (per the permit-adapter skill) whether to narrow the radius, harvest roofing permits first, or go ahead anyway.

## Identity baseline note

`county-permit-adapter` says not to harvest until the official corporate registry and an adequate licensing snapshot are loaded. The NYS DOS corporations are loaded (409,304); the Nassau Consumer Affairs licence list is not. Harvesting here captures contacts **raw only**: no company, person, or licence is stamped from portal names, and roofing classification uses permit text and role/name signals, not identity.
