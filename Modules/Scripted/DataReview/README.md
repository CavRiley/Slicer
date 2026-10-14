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
(`Modules/Scripted/DataReview`), and the dataset folder.

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
- *Show segmentation* and *Reset views* are below the navigation buttons.

| Key | Action |
| --- | --- |
| `A` | Accept and advance |
| `R` | Reject and advance (needs a comment) |
| `N` / `→` | Next case |
| `P` / `←` | Previous case |
| `U` | Next unreviewed case |
| `S` | Show / hide the segmentation |
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
