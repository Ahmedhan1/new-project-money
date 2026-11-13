# Installing and managing assets

Nothing is installed with NEXUS except the catalog. Built-in assets (effects, transitions, motion presets) are renderer
features and are always available; files are created or downloaded only when you install them.

## Where they go
`NEXUS_HOME/assets/` (`.nexus/assets` by default):
```
effects/  transitions/  luts/  frames/  overlays/  titles/  templates/  animations/  fonts/  sound_effects/  music/
thumbnails/   cached previews (re-created on demand)
manifests/    installed.json (SHA-256 + size + source per asset), favorites.json, user_assets.json
tmp/          downloads in progress (*.part), pack archives before extraction
```
Never committed to Git (`.nexus/` is ignored); the repository keeps only the manifest, code, tests and docs.

## Install
- UI: **Asset Library** -> filter / search -> **Install** (downloads run as a job with progress) -> preview appears.
- Workspace: the **Assets** card lists a category; **Clip** applies to the selected clip, **Timeline** to every clip or
  the whole programme. Installing happens on first apply.
- CLI: `python -m nexus library list --category luts` · `python -m nexus library install lut.cinematic font.inter`.
- API: `POST /api/library/install {"asset_id": ...}`.

What install does:
1. licence check (allowed licence, commercial use, author/source/attribution; pinned URL + SHA-256 for downloads);
2. disk preflight (`diskman.preflight`);
3. generate locally, or download to `<file>.part` with HTTP Range resume (an interrupted download continues where it
   stopped), then compare the SHA-256 with the pinned value - a mismatch deletes the file;
4. OFL fonts: the licence text is downloaded too (`<id>.LICENSE.txt`, also pinned);
5. packs: extract only the listed members, flattened into the asset folder (no path from the archive is trusted);
   the archive itself is deleted;
6. record SHA-256, size, source and licence in `manifests/installed.json`; flag a duplicate of an existing asset.

## Verify, repair, remove
- **Verify checksums** (UI) / `python -m nexus library verify`: every installed file against its recorded SHA-256 (and
  downloads against the pinned SHA-256).
- **Repair missing**: files a timeline uses but that are gone (deleted, evicted, restored project) are re-installed.
- **Remove**: refused while any project timeline uses the file; deletes only the files that install wrote.
- **Quota**: above `asset_cache_quota_gb` the least recently used *re-creatable* assets are removed; favourites, your
  own imports and anything a timeline uses are kept.

## Your own assets
UI "Import your own asset" or `POST /api/library/import`: you must give the licence (SPDX id), author, source and
attribution, and confirm commercial use; licences outside the allowed list are refused. The same bytes imported twice
are one asset (SHA-256). **Export favourites** writes a zip with the files and a manifest (licence + SHA-256 per file);
importing a pack re-checks every checksum and licence.

## Adding a catalog asset (developers)
Add an entry to `nexus/registry/asset_library.yaml`. Generated assets need a generator in
`nexus/assetlib/generators.py`; downloads need `url`, `sha256`, `size`, `license`, `author`, `source_url`,
`attribution`, `commercial_use: true` (and `license_url` + `license_sha256` for OFL/MIT/Apache files), and their host
must be declared in `nexus/registry/components.yaml`. Then run `python scripts/maintenance/gen_asset_docs.py` and
`pytest tests/fast/test_a2_assetlib.py`.
