"""One-time setup of a review machine. Run from inside Slicer (needs no source build):

    Slicer --python-script /path/to/DataReview/Scripts/setup_review_station.py \
        --manifest /data/stroke/review_manifest.json --reviewer "JD"

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


def main():
    parser = argparse.ArgumentParser(description="Configure this machine as a Data Review station")
    parser.add_argument("--manifest", help="review_manifest.json to open automatically at startup")
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

    if not args.manifest:
        parser.error("--manifest is required (or use --reset)")
    manifestPath = os.path.abspath(args.manifest)
    if not os.path.isfile(manifestPath):
        print(f"ERROR: manifest not found: {manifestPath}")
        slicer.util.exit(1)

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
