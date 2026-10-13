# Data Review module

A 3D Slicer module for stepping through a dataset of images and their segmentations
(for example stroke lesion masks) and recording an **accept / reject** decision and a
**comment** for each case. Progress is stored in a single JSON *manifest* file that lives
next to the data, so a review can be paused, resumed, moved, or continued on another machine.

## Installation

The module is a self-contained folder: `Modules/Scripted/DataReview`. There are three ways to use it.

1. **Installed Slicer, no build (recommended).** Copy or clone this folder onto the machine, then in
   Slicer open *Edit → Application Settings → Modules*, add the folder to *Additional module paths*,
   and restart Slicer. The module appears under *Modules → Utilities → Data Review*.
   Tested with Slicer 5.12.3; it uses only standard Slicer Python APIs.
2. **Source build.** The module is registered in `Modules/Scripted/CMakeLists.txt`, so a Slicer built
   from this branch includes it automatically.
3. **Command line.** Point Slicer at the folder for a single session:

   ```
   Slicer --additional-module-paths /path/to/DataReview
   ```

## Expected data layout

Images and segmentations are matched by **base name**. The default patterns assume:

```
dataset/
  images/sub-001.nii.gz
  images/sub-002.nii.gz
  labels/sub-001_seg.nii.gz
  labels/sub-002_seg.nii.gz
  review_manifest.json      <- created by the module
```

Both patterns are glob expressions relative to the dataset folder and support `**` for
sub-folders. Suffixes such as `_seg`, `_label`, `_mask`, `_lesion`, `_gt` on segmentation
files are ignored when pairing. For per-case folders (`sub-001/image.nii.gz` and
`sub-001/seg.nii.gz`) use patterns like `*/image.nii.gz` and `*/seg.nii.gz`; the folder name
becomes the case id. Any format Slicer can read works (NIfTI, NRRD, MHA, ...). All images are
assumed to share one physical space, so slice positions are kept between cases.

## Workflow

1. **Reviewer**: enter your name or initials. It is saved with every decision and remembered between sessions.
2. **Create a manifest**: expand *Create manifest from folder*, pick the dataset folder, adjust the two
   patterns if needed, and press *Scan folder and create manifest*. This writes `review_manifest.json`
   into the dataset folder and opens the first unreviewed case. Re-scanning the same folder keeps
   existing decisions and comments.
3. **Load an existing manifest** instead with *Manifest file → Load*. The review resumes at the first
   unreviewed case.
4. **Review**: look at the image and overlay, type a comment if needed, then press *Accept* or *Reject*.
   The decision is written to disk immediately and the next unreviewed case opens (uncheck
   *Advance to next unreviewed case* to stay). *Clear* sets a case back to pending. Comments are saved
   whenever you leave a case, even without a decision.
5. **Navigate** with the buttons, by clicking a case in the list, or with the keyboard.

| Key | Action |
| --- | --- |
| `A` | Accept and advance |
| `R` | Reject and advance |
| `N` / `→` | Next case |
| `P` / `←` | Previous case |
| `U` | Next unreviewed case |
| `S` | Toggle segmentation visibility |

Shortcuts are active only while the module is open and are ignored while typing in a text field.
The *Display* section has a fill-opacity slider, the keep-slice-positions toggle, and a
*Reset views* button if the field of view drifts.

## The manifest file

```json
{
  "format": "SlicerDataReview",
  "version": 1,
  "root": ".",
  "cases": [
    {
      "id": "sub-001",
      "image": "images/sub-001.nii.gz",
      "segmentation": "labels/sub-001_seg.nii.gz",
      "status": "accepted",
      "comment": "small lesion, mask slightly over-segmented",
      "reviewer": "cr",
      "reviewed_at": "2026-10-06T01:10:16+00:00"
    }
  ]
}
```

- `status` is one of `pending`, `accepted`, `rejected`. `reviewed_at` is a UTC timestamp.
- `image` and `segmentation` are resolved against `root`; a relative `root` is resolved against the
  folder containing the manifest. `segmentation` may be `null`.
- The file is rewritten atomically (temp file + rename) after every change, so a crash never leaves
  it half-written.
- The manifest can also be written by hand or by a script; only `id` and `image` are required per case.

## Using it across machines

Because every path in the manifest is relative to the manifest file, the whole dataset folder
(images, segmentations, and `review_manifest.json`) can be copied, synced, or mounted anywhere and
opened with *Load* without edits. Practical notes:

- Keep the manifest **inside** the dataset folder. If you store it elsewhere, set `root` to the
  dataset path (absolute, or relative to the manifest).
- The module never merges concurrent edits. For several reviewers, give each their own copy
  (for example `review_manifest_cr.json`) and compare them afterwards with a script. Two people
  writing the same file at the same time over a shared drive will overwrite each other.
- The manifest is plain JSON and diffs cleanly, so it can be versioned with Git alongside a small
  dataset or a data-tracking tool such as DVC for larger ones.
- Reviewer name, last manifest path, and the two glob patterns are stored in Slicer's own
  settings on each machine, not in the manifest.

## Summarizing results

The manifest is easy to post-process. For example, to list rejected cases:

```python
import json
m = json.load(open("review_manifest.json"))
for c in m["cases"]:
    if c["status"] == "rejected":
        print(c["id"], "-", c["comment"])
```

## Running the self-test

The module ships a test that builds a synthetic dataset, scans it, loads cases, and checks the
manifest round trip. Run it from Slicer's Python console with
`slicer.util.selectModule("DataReview")` followed by the *Reload and Test* button in developer mode,
or headlessly:

```
Slicer --no-main-window --testing --additional-module-paths /path/to/DataReview \
  --python-code "import DataReview, slicer; DataReview.DataReviewTest().runTest(); slicer.util.exit(0)"
```
