# Data Review module

A 3D Slicer module for reviewing a dataset of diffusion MRI cases (ADC and trace-weighted images)
with their stroke lesion segmentations. Both images are loaded into axial, sagittal and coronal views
with the lesion overlaid; a slider switches the view between ADC and TRACEW. The reviewer presses
**Accept** or **Reject** (with a comment) for each case.
Progress is stored in a JSON *manifest* next to the data, so a review can be paused, resumed, or
moved to another machine.

The module is designed so that a technical person sets up a machine once, and a non-technical
reviewer then only starts Slicer and presses buttons.

## Setting up a review machine

You need: an installed Slicer (tested with 5.12.3, no source build required), this module folder
(`Modules/Scripted/DataReview`), and the dataset folder with its `review_manifest.json`.

### Quick way (macOS / Linux)

Copy the dataset folder onto the machine, then run from a terminal:

```
curl -fsSL https://raw.githubusercontent.com/CavRiley/Slicer/data-review-module/Modules/Scripted/DataReview/Scripts/install_review_station.sh \
  | bash -s -- --manifest /path/to/dataset/review_manifest.json --reviewer "JD"
```

This fetches only the module folder from the repository into `~/DataReview` (a sparse git clone, about
1 MB), finds the installed Slicer, and runs the setup described below. Add `--install-slicer` on macOS
to download the latest stable Slicer into `/Applications` if none is installed, `--full-ui` to keep
Slicer's menus and toolbars, `--reset` to undo the setup, and `--dest DIR` to keep the checkout
elsewhere. Running it again later updates the module with `git pull`. Run `bash install_review_station.sh --help`
for all options. If the dataset has no manifest yet, run the script with `--skip-setup`, create the manifest
in Slicer (step 2 below), then run the script again with `--manifest`.

### Quick way (Windows)

Copy the dataset folder onto the machine, install [git for Windows](https://git-scm.com/download/win)
if it is missing, then run in PowerShell:

```
powershell -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((irm https://raw.githubusercontent.com/CavRiley/Slicer/data-review-module/Modules/Scripted/DataReview/Scripts/install_review_station.ps1))) -Manifest 'D:\dataset\review_manifest.json' -Reviewer 'JD'"
```

It does the same as the macOS/Linux script: sparse clone into `%USERPROFILE%\DataReview`, Slicer
detection (per-user installs under `%LOCALAPPDATA%\slicer.org` and `Program Files`), and the setup.
Options mirror the shell script: `-InstallSlicer` downloads and silently installs the latest stable
Slicer if none is found, `-FullUI`, `-SkipSetup`, `-Reset`, `-Dest`, `-Slicer 'C:\path\Slicer.exe'`.
Slicer's own messages go to its log file rather than the console on Windows; the script reports the
outcome from Slicer's exit code.

### Step by step

1. **Copy** the `DataReview` folder and the dataset folder onto the machine.
2. **Create the manifest** once (skip if the dataset folder already contains `review_manifest.json`):
   start Slicer, add the module folder under *Edit → Application Settings → Modules → Additional
   module paths*, restart, open *Modules → Utilities → Data Review*, expand *Dataset and setup →
   Create manifest from folder*, pick the dataset folder, check the three patterns, and press
   *Scan folder and create manifest*. The manifest is written into the dataset folder.
3. **Configure startup** by running the setup script once from a terminal (Slicer exits when it is done):

   ```
   # macOS
   /Applications/Slicer.app/Contents/MacOS/Slicer --python-script /path/to/DataReview/Scripts/setup_review_station.py \
       --manifest /path/to/dataset/review_manifest.json --reviewer "JD"

   # Windows
   "C:\Program Files\Slicer 5.12.3\Slicer.exe" --python-script C:\path\to\DataReview\Scripts\setup_review_station.py ^
       --manifest D:\dataset\review_manifest.json --reviewer "JD"
   ```

   This registers the module path, makes Data Review the module that opens at startup, opens the
   manifest automatically, stores the reviewer name, and switches on the simplified interface.
   Add `--full-ui` to keep the normal Slicer menus and toolbars, or `--reset` to undo everything.

   The same can be done by hand: set the home module in *Edit → Application Settings → Modules*,
   and in the module tick *Open this manifest automatically when Slicer starts* and
   *Simplified interface at startup*.

4. **Start Slicer.** It opens directly in Data Review, loads the first unreviewed case, and shows
   nothing but the review panel and the two image views. Settings are per user account, so run the
   setup under the account the reviewer will use.

Press **Ctrl+Shift+U** at any time to bring back the full Slicer interface (and again to hide it).

### A lighter Slicer

The simplified interface hides the other modules rather than removing them, which is enough for
reviewers and keeps the install a plain download. If a genuinely stripped-down build is wanted later,
Slicer supports that from this source tree with the CMake options `Slicer_QTLOADABLEMODULES_DISABLED`,
`Slicer_QTSCRIPTEDMODULES_DISABLED` and `Slicer_CLIMODULES_DISABLED`, or by packaging the module as a
[Slicer custom application](https://github.com/KitwareMedical/SlicerCustomAppTemplate).

## Expected data layout

Images and segmentations are matched by **case name**. The default patterns assume:

```
dataset/
  adc/sub-001_adc.nii.gz
  tracew/sub-001_tracew.nii.gz
  labels/sub-001_seg.nii.gz
  ...
  review_manifest.json      <- created by the module
```

The three patterns are glob expressions relative to the dataset folder and support `**` for
sub-folders. When pairing, the extension and a trailing channel or label suffix (`_adc`, `_tracew`,
`_dwi`, `_b1000`, `_seg`, `_label`, `_mask`, `_lesion`, `_gt`, ...) are removed, so `sub-001_adc.nii.gz`,
`sub-001_tracew.nii.gz` and `sub-001_seg.nii.gz` all belong to case `sub-001`. For per-case folders
such as `sub-001/adc.nii.gz` the folder name is the case id; use patterns like `*/adc.nii.gz`.
Any format Slicer reads works (NIfTI, NRRD, MHA, ...). Cases missing one channel or the
segmentation are still listed and the missing part is simply not shown.

All images are assumed to share one physical space, so slice positions are kept between cases.

## Reviewing

The panel shows, from top to bottom: progress, the case list (green ✓ accepted, red ✗ rejected,
💬 has a comment), the current case, the comment box, the big **ACCEPT** / **REJECT** buttons, and
the navigation buttons.

- **Accept** records the decision and moves to the next unreviewed case.
- **Reject** requires a comment describing why. Without one the comment box turns red and the
  decision is not recorded.
- Comments are saved whenever you leave a case, with or without a decision.
- **Remove decision** sets a reviewed case back to unreviewed (the comment is kept).
- The three views are fixed to axial (large, left), sagittal and coronal (right) and are linked, so
  scrolling, zooming, or changing layers in one view applies to the others.
- Both images are in every view: ADC as the background layer, TRACEW as the foreground layer. The
  *ADC ◀ ▶ TRACEW* slider in the panel (or the opacity slider in any view controller, or the `T` key)
  blends between them: 0 shows only ADC, 1 shows only TRACEW. The segmentation is overlaid on top.
- The *Lesion* box switches the segmentation between **Filled**, **Outline only** and **Hidden**; the
  `S` key cycles through the three. *Reset views* re-centres all views on the current images.

| Key | Action |
| --- | --- |
| `A` | Accept and advance |
| `R` | Reject and advance (needs a comment) |
| `N` / `→` | Next case |
| `P` / `←` | Previous case |
| `U` | Next unreviewed case |
| `S` | Lesion: filled, outline only, hidden |
| `T` | Switch between ADC and TRACEW |
| `Ctrl+Shift+U` | Show / hide the full Slicer interface |

Shortcuts are active only while the module is open and are ignored while typing in a text field.

## The manifest file

```json
{
  "format": "SlicerDataReview",
  "version": 2,
  "root": ".",
  "channels": ["ADC", "TRACEW"],
  "cases": [
    {
      "id": "sub-001",
      "images": {
        "ADC": "adc/sub-001_adc.nii.gz",
        "TRACEW": "tracew/sub-001_tracew.nii.gz"
      },
      "segmentation": "labels/sub-001_seg.nii.gz",
      "status": "rejected",
      "comment": "mask extends into CSF",
      "reviewer": "JD",
      "reviewed_at": "2026-10-13T17:18:06+00:00"
    }
  ]
}
```

- `status` is `pending`, `accepted` or `rejected`; `reviewed_at` is a UTC timestamp.
- `images` maps channel name to file; the first entry of `channels` is the background layer and the
  second the foreground layer. `segmentation` or a channel may be `null`.
- Paths are resolved against `root`; a relative `root` is relative to the manifest's folder.
- The file is rewritten atomically after every change (temporary file, then rename).
- Only `id` and `images` are required per case, so a manifest can also be produced by a script.
  Older version-1 manifests with a single `image` entry are upgraded automatically on load.

## Moving a review between machines

All paths in the manifest are relative to the manifest file, so the whole dataset folder (images,
segmentations and `review_manifest.json`) can be copied, synced, or mounted anywhere and opened
without edits. Practical notes:

- Keep the manifest **inside** the dataset folder. If it must live elsewhere, set `root` to the
  dataset path.
- The module never merges concurrent edits. For several reviewers, give each their own copy
  (for example `review_manifest_jd.json`) and compare afterwards. Two people writing the same
  file at once over a shared drive will overwrite each other.
- The manifest is plain JSON and diffs cleanly, so it can be versioned with Git next to a small
  dataset, or with DVC for larger ones.
- Reviewer name, startup manifest, interface mode and glob patterns are Slicer settings on each
  machine, not part of the manifest. Re-run the setup script on each new machine.

## Troubleshooting

- **Slicer starts without toolbars or a module selector.** Slicer saves the window layout on exit.
  Current versions of the module restore the toolbars before that happens, but if it occurs, open Data
  Review and press `Ctrl+Shift+U`, or run the setup script with `--reset`, or delete the
  `windowState` line under `[MainWindow]` in `Slicer.ini` (shown by `Slicer --settings-path`).
- **`Error #1 while writing setting "Modules/AdditionalPaths"` at startup.** The installed Slicer keeps
  its revision-specific settings in a `slicer.org` folder next to the application and prints this
  warning on every start. It is harmless: the values are still written (check with `Slicer --settings-path`
  for the user file; the revision file is `Slicer-<revision>.ini` in that folder).
- **The setup script prints `WARNING: could not save the module path`.** Slicer keeps the module
  path list inside its application folder. On macOS a terminal without the *App Management* privacy
  permission cannot write there. Either grant it (System Settings → Privacy & Security → App
  Management), or add the printed folder by hand in Slicer under *Edit → Application Settings →
  Modules → Additional module paths* and restart. Everything else the script sets is already in place.
- **Slicer opens in the Welcome module instead of Data Review.** Re-run the setup script; the home
  module is stored in Slicer's user settings file, which is also where *Edit → Application Settings →
  Modules → Default startup module* writes it.

## Summarizing results

```python
import json
m = json.load(open("review_manifest.json"))
for c in m["cases"]:
    if c["status"] == "rejected":
        print(c["id"], "-", c["comment"])
```

## Running the self-test

The module ships a test that builds a synthetic two-channel dataset, scans it, loads cases into the
review layout, and checks the manifest round trip. In Slicer, enable developer mode
(*Edit → Application Settings → Developer*) and press *Reload and Test* in the module, or run headlessly:

```
Slicer --no-main-window --testing --additional-module-paths /path/to/DataReview \
  --python-code "import DataReview, slicer; DataReview.DataReviewTest().runTest(); slicer.util.exit(0)"
```
