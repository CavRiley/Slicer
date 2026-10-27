"""One-time setup of a review machine. Run from inside Slicer (needs no source build):

    Slicer --python-script /path/to/DataReview/Scripts/setup_review_station.py \
        --manifest /data/stroke/review_manifest.json --reviewer "JD"

or, when the dataset has no manifest yet but follows the default layout (adc/, tracew/, labels/):

    Slicer --python-script /path/to/DataReview/Scripts/setup_review_station.py \
        --dataset /data/stroke --reviewer "JD"

which scans the folder, writes /data/stroke/review_manifest.json (keeping any decisions already in it),
and then configures Slicer to open it. --adc-pattern, --tracew-pattern and --seg-pattern override the layout.

What it does (all stored in Slicer's own settings for this user):
  * registers the DataReview folder as an additional module path,
  * makes Data Review the module that opens at startup,
  * tells Data Review to open the given manifest automatically,
  * turns on the simplified interface (no menus, toolbars, help) unless --full-ui is given,
  * optionally stores the reviewer name.

Slicer exits when the script is done; start it normally afterwards.
Use --reset to undo everything.
"""
import argparse
import os
import sys

import qt
import slicer


def createManifest(moduleDir, datasetDir, adcPattern, tracewPattern, segPattern):
    """Scan a dataset folder with the module's own logic and write review_manifest.json into it."""
    import json
    datasetDir = os.path.abspath(datasetDir)
    if not os.path.isdir(datasetDir):
        print(f"ERROR: dataset folder not found: {datasetDir}")
        return None
    if moduleDir not in sys.path:
        sys.path.insert(0, moduleDir)
    import DataReview
    manifestPath = os.path.join(datasetDir, "review_manifest.json")
    try:
        manifest = DataReview.DataReviewLogic.scanDirectory(
            datasetDir, {"ADC": adcPattern, "TRACEW": tracewPattern}, segPattern or None)
    except Exception as e:
        print(f"ERROR: could not create the manifest: {e}")
        return None
    previous = None
    if os.path.isfile(manifestPath):
        with open(manifestPath, encoding="utf-8") as f:
            previous = json.load(f)
        manifest = DataReview.DataReviewLogic.mergeReviewState(manifest, previous)
    logic = DataReview.DataReviewLogic()
    logic.setManifest(manifest, manifestPath)
    logic.save()
    counts = logic.summary()
    missing = sum(1 for c in logic.cases if not c.get("segmentation"))
    print(f"Manifest {'updated' if previous else 'created'}: {manifestPath}")
    print(f"  {counts['total']} cases ({counts['reviewed']} already reviewed), {missing} without segmentation")
    return manifestPath


def main():
    parser = argparse.ArgumentParser(description="Configure this machine as a Data Review station")
    parser.add_argument("--manifest", help="review_manifest.json to open automatically at startup")
    parser.add_argument("--dataset", help="dataset folder: scan it and create review_manifest.json in it (alternative to --manifest)")
    parser.add_argument("--adc-pattern", default="adc/*.nii.gz", help="glob for ADC images, relative to the dataset folder")
    parser.add_argument("--tracew-pattern", default="tracew/*.nii.gz", help="glob for TRACEW images, relative to the dataset folder")
    parser.add_argument("--seg-pattern", default="labels/*.nii.gz", help="glob for lesion segmentations, relative to the dataset folder")
    parser.add_argument("--reviewer", default=None, help="reviewer name stored with each decision")
    parser.add_argument("--full-ui", action="store_true", help="keep the regular Slicer interface (menus, toolbars)")
    parser.add_argument("--reset", action="store_true", help="remove all Data Review startup settings")
    args = parser.parse_args(sys.argv[1:])

    moduleDir = os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[0])))  # Scripts/ -> module folder
    # Slicer keeps "Modules/AdditionalPaths" in the revision-specific settings file, but the home module and
    # everything else in the regular user settings file.
    revisionSettings = slicer.app.revisionUserSettings()
    settings = slicer.app.userSettings()

    additionalPaths = list(revisionSettings.value("Modules/AdditionalPaths") or [])
    if isinstance(additionalPaths, str):
        additionalPaths = [additionalPaths]

    if args.reset:
        additionalPaths = [p for p in additionalPaths if os.path.normpath(p) != os.path.normpath(moduleDir)]
        revisionSettings.setValue("Modules/AdditionalPaths", additionalPaths)
        settings.setValue("Modules/HomeModule", "Welcome")
        for key in ("DataReview/autoLoadManifest", "DataReview/simplifiedUI", "MainWindow/windowState"):
            settings.remove(key)  # dropping windowState brings back any toolbars hidden by the simplified interface
        revisionSettings.sync()
        settings.sync()
        print("Data Review startup settings removed.")
        slicer.util.exit(0)
        return

    if args.dataset and not args.manifest:
        args.manifest = createManifest(moduleDir, args.dataset, args.adc_pattern, args.tracew_pattern, args.seg_pattern)
        if not args.manifest:
            slicer.util.exit(1)
            return
    if not args.manifest:
        parser.error("--manifest or --dataset is required (or use --reset)")
    manifestPath = os.path.abspath(args.manifest)
    if not os.path.isfile(manifestPath):
        print(f"ERROR: manifest not found: {manifestPath}")
        slicer.util.exit(1)
        return

    if os.path.normpath(moduleDir) not in [os.path.normpath(p) for p in additionalPaths]:
        additionalPaths.append(moduleDir)
    revisionSettings.setValue("Modules/AdditionalPaths", additionalPaths)
    settings.setValue("Modules/HomeModule", "DataReview")
    settings.remove("MainWindow/windowState")  # start from the default window layout (all toolbars visible)
    settings.setValue("DataReview/autoLoadManifest", manifestPath)
    settings.setValue("DataReview/simplifiedUI", not args.full_ui)
    if args.reviewer is not None:
        settings.setValue("DataReview/reviewer", args.reviewer)
    revisionSettings.sync()
    settings.sync()

    # Verify that the module path really reached the revision settings file. Slicer keeps that file next to
    # the application; on macOS a shell without "App Management" permission cannot write inside the bundle.
    # (Read the file directly: QSettings objects in the same process share a cache and would not notice.)
    savedText = ""
    try:
        with open(revisionSettings.fileName(), encoding="utf-8") as f:
            savedText = f.read()
    except OSError:
        pass
    if moduleDir not in savedText:
        print("WARNING: could not save the module path to", revisionSettings.fileName())
        print("         Add it by hand in Slicer: Edit > Application Settings > Modules > Additional module paths:")
        print("         " + moduleDir)
        print("         (On macOS, running this script from a terminal that has 'App Management' permission,")
        print("         or from Slicer's own Python console, also works.)")
        slicer.util.exit(3)
        return

    print("Data Review station configured:")
    print(f"  module folder : {moduleDir}")
    print(f"  manifest      : {manifestPath}")
    print(f"  interface     : {'full' if args.full_ui else 'simplified (Ctrl+Shift+U toggles)'}")
    if args.reviewer is not None:
        print(f"  reviewer      : {args.reviewer}")
    print("Start Slicer normally to begin reviewing.")
    slicer.util.exit(0)


if __name__ == "__main__":
    main()
