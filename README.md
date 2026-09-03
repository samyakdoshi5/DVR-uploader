# FPV YT Uploader

Uploads every video under `F:\Media\FPV DVRs` to YouTube as unlisted videos.

The first folder under the root is treated as the craft name. The second folder, when present, is treated as the location. Deeper folders are scanned for dates and day labels, but they do not replace the location.

Examples:

- `F:\Media\FPV DVRs\F450\F450_001.mov`
  - craft: `F450`
  - location: none
- `F:\Media\FPV DVRs\Goblin\VIT\Goblin_001 (2).mov`
  - craft: `Goblin`
  - location: `VIT`
- `F:\Media\FPV DVRs\Hummy\Home\2026-06-18\VID00181.mov`
  - craft: `Hummy`
  - location: `Home`
  - date: `2026-06-18`
- `F:\Media\FPV DVRs\Hummy\Singh AeroFarm HYD\2026-08-16 WingFest\VID00273.mov`
  - craft: `Hummy`
  - location: `Singh AeroFarm HYD`
  - date: `2026-08-16`
  - day label: `WingFest`

## Playlists

Each uploaded video is added to:

- a craft playlist, named like `Hummy`
- a location playlist when a location folder exists, named like `Home`
- a day playlist when a dated folder exists, named like `16/08/26 WingFest`

Videos directly inside a craft folder do not get a location playlist.

## Titles

Dated videos are titled as `SSS || DD/MM/YY || Label || Craft || Location`. Missing fields are skipped:

```text
001 || 18/06/26 || Hummy || Home
```

Dated videos with a label in the folder name include that label:

```text
001 || 16/08/26 || WingFest || Hummy || Singh AeroFarm HYD
```

Undated videos are titled:

```text
001 || F450
```

Dates and labels are read only from folder names. The script does not read video metadata for dates. Folder names like `2026-06-30 (Terrace)` and `2026-08-31 Reddy flying + Indra LEDs` become labels `Terrace` and `Reddy flying + Indra LEDs`.

Serial numbers are assigned in natural DVR filename order:

- dated clips: within the detected date bucket
- undated clips: across all undated clips for that craft

## Setup

Install dependencies:

```powershell
pip install -r "F:\Media\FPV DVRs\FPV YT Uploader\requirements.txt"
```

Put the Google OAuth desktop-app JSON for the target channel in this folder. The filename must start with `client_secret` and end with `.json`.

Authenticate only:

```powershell
python "F:\Media\FPV DVRs\FPV YT Uploader\upload_to_youtube.py" --auth-only
```

Preview everything without uploading:

```powershell
python "F:\Media\FPV DVRs\FPV YT Uploader\upload_to_youtube.py" --dry-run
```

By default, files smaller than 50 MB are skipped. You can change that cutoff:

```powershell
python "F:\Media\FPV DVRs\FPV YT Uploader\upload_to_youtube.py" --dry-run --min-file-size-mb 100
```

Upload after reviewing the plan:

```powershell
python "F:\Media\FPV DVRs\FPV YT Uploader\upload_to_youtube.py"
```

Skip confirmation:

```powershell
python "F:\Media\FPV DVRs\FPV YT Uploader\upload_to_youtube.py" --yes
```

Process only one craft or location:

```powershell
python "F:\Media\FPV DVRs\FPV YT Uploader\upload_to_youtube.py" --craft Hummy
python "F:\Media\FPV DVRs\FPV YT Uploader\upload_to_youtube.py" --location "Singh AeroFarm HYD"
```

If you need Google to ask for permissions again:

```powershell
python "F:\Media\FPV DVRs\FPV YT Uploader\upload_to_youtube.py" --reauth --auth-only
```

## Notes

- Nothing is deleted or moved.
- Files smaller than 50 MB are skipped by default.
- `state.json` is created automatically and tracks uploaded files.
- `token.json` is created after the first login.
- Add exact folder names to `ignored_folders.txt` if you want to skip them.
