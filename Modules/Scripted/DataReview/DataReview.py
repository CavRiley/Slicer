import datetime
import glob
import json
import logging
import os
import re
import tempfile

import ctk
import qt
import vtk

import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin


#
# DataReview
#


class DataReview(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("Data Review")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "Utilities")]
        self.parent.dependencies = []
        self.parent.contributors = ["Cavan Riley"]
        self.parent.helpText = _("""
<p>Step through a dataset of images and their segmentations (for example stroke lesion masks),
and accept, reject, or comment on each case.</p>

<p>The list of cases and the review state (status, comment, reviewer, timestamp) live in a single
JSON <i>manifest</i> file that is saved automatically after every change. A manifest can be created by
scanning a dataset folder with an image and a segmentation glob pattern; files are paired by base name.</p>

<p>Images are assumed to share the same physical space, so slice positions are kept between cases
by default.</p>

<p>Keyboard shortcuts (active while this module is open): <b>A</b> accept, <b>R</b> reject,
<b>N</b>/<b>Right</b> next, <b>P</b>/<b>Left</b> previous, <b>U</b> next unreviewed,
<b>S</b> toggle segmentation visibility.</p>
""")
        self.parent.acknowledgementText = _("""
Developed for reviewing imaging datasets and their stroke lesion segmentations.
""")


#
# Review status constants
#

STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"
ALL_STATUSES = (STATUS_PENDING, STATUS_ACCEPTED, STATUS_REJECTED)

MANIFEST_FORMAT = "SlicerDataReview"
MANIFEST_VERSION = 1

# Suffixes commonly appended to segmentation file names; stripped before pairing with images.
SEGMENTATION_SUFFIXES = ("_seg", "-seg", "_segmentation", "_label", "_labels", "_mask", "_lesion", "_gt")


def _stripImageExtensions(filename):
    """Return the base name of an image file without its (possibly double) extension."""
    name = os.path.basename(filename)
    for ext in (".nii.gz", ".nrrd", ".nhdr", ".nii", ".mha", ".mhd", ".seg.nrrd", ".img.gz", ".img", ".hdr"):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return os.path.splitext(name)[0]


def _utcTimestamp():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


#
# DataReviewLogic
#


class DataReviewLogic(ScriptedLoadableModuleLogic):
    """Manages the review manifest (JSON) and loads cases into the scene.

    The manifest is a dictionary of the form::

        {
          "format": "SlicerDataReview",
          "version": 1,
          "root": "/path/to/dataset",            # base for relative case paths (optional)
          "cases": [
            {
              "id": "sub-001",
              "image": "images/sub-001.nii.gz",     # absolute, or relative to root
              "segmentation": "labels/sub-001.nii.gz",  # may be null
              "status": "pending" | "accepted" | "rejected",
              "comment": "",
              "reviewer": "",
              "reviewed_at": null | ISO-8601 timestamp
            }
          ]
        }
    """

    def __init__(self):
        ScriptedLoadableModuleLogic.__init__(self)
        self.manifestPath = None
        self.manifest = None
        self.currentIndex = -1
        self.imageNode = None
        self.segmentationNode = None

    # ---- Manifest creation / IO ----------------------------------------------------------------

    @staticmethod
    def emptyManifest(root=None):
        return {"format": MANIFEST_FORMAT, "version": MANIFEST_VERSION, "root": root, "cases": []}

    @staticmethod
    def scanDirectory(rootDirectory, imagePattern, segmentationPattern=None):
        """Build a manifest by matching image and segmentation files by base name.

        :param rootDirectory: dataset folder; patterns are evaluated relative to it.
        :param imagePattern: glob pattern for the images, e.g. ``images/*.nii.gz`` or ``**/t1.nii.gz``.
        :param segmentationPattern: glob pattern for the segmentations (optional).
        :return: manifest dictionary (not saved). Case paths are stored relative to ``rootDirectory``.
        """
        rootDirectory = os.path.abspath(rootDirectory)
        if not os.path.isdir(rootDirectory):
            raise ValueError(f"Dataset folder does not exist: {rootDirectory}")

        imageFiles = sorted(glob.glob(os.path.join(rootDirectory, imagePattern), recursive=True))
        imageFiles = [f for f in imageFiles if os.path.isfile(f)]
        if not imageFiles:
            raise ValueError(f"No images found for pattern '{imagePattern}' in {rootDirectory}")

        segmentationByKey = {}
        if segmentationPattern:
            for f in sorted(glob.glob(os.path.join(rootDirectory, segmentationPattern), recursive=True)):
                if not os.path.isfile(f):
                    continue
                key = _stripImageExtensions(f)
                for suffix in SEGMENTATION_SUFFIXES:
                    if key.lower().endswith(suffix):
                        key = key[: -len(suffix)]
                        break
                segmentationByKey.setdefault(key, f)
                # Also index by parent folder name, for layouts like case001/image.nii.gz + case001/seg.nii.gz
                segmentationByKey.setdefault("dir:" + os.path.basename(os.path.dirname(f)), f)

        manifest = DataReviewLogic.emptyManifest(rootDirectory)
        usedIds = set()
        for imageFile in imageFiles:
            stem = _stripImageExtensions(imageFile)
            parentName = os.path.basename(os.path.dirname(imageFile))
            segmentationFile = segmentationByKey.get(stem)
            caseId = stem
            if segmentationFile is None and parentName and os.path.dirname(imageFile) != rootDirectory:
                segmentationFile = segmentationByKey.get("dir:" + parentName)
                caseId = parentName
            if caseId in usedIds:
                caseId = os.path.relpath(imageFile, rootDirectory)
            usedIds.add(caseId)
            manifest["cases"].append({
                "id": caseId,
                "image": os.path.relpath(imageFile, rootDirectory),
                "segmentation": os.path.relpath(segmentationFile, rootDirectory) if segmentationFile else None,
                "status": STATUS_PENDING,
                "comment": "",
                "reviewer": "",
                "reviewed_at": None,
            })
        return manifest

    @staticmethod
    def mergeReviewState(newManifest, oldManifest):
        """Copy status/comment/reviewer from ``oldManifest`` into ``newManifest`` for cases with the same id."""
        if not oldManifest:
            return newManifest
        oldById = {c["id"]: c for c in oldManifest.get("cases", [])}
        for case in newManifest["cases"]:
            old = oldById.get(case["id"])
            if old:
                for key in ("status", "comment", "reviewer", "reviewed_at"):
                    if key in old:
                        case[key] = old[key]
        return newManifest

    def load(self, manifestPath):
        with open(manifestPath, encoding="utf-8") as f:
            manifest = json.load(f)
        if not isinstance(manifest, dict) or "cases" not in manifest:
            raise ValueError(f"Not a valid review manifest: {manifestPath}")
        self.validate(manifest)
        self.manifest = manifest
        self.manifestPath = os.path.abspath(manifestPath)
        self.currentIndex = -1
        logging.info(f"DataReview: loaded {len(manifest['cases'])} cases from {self.manifestPath}")
        return manifest

    def setManifest(self, manifest, manifestPath):
        """Adopt an in-memory manifest that will be saved to ``manifestPath``.

        An absolute ``root`` is rewritten relative to the manifest folder so that the manifest keeps
        working when the dataset folder is moved or opened on another machine.
        """
        self.validate(manifest)
        self.manifestPath = os.path.abspath(manifestPath)
        root = manifest.get("root")
        if root and os.path.isabs(root):
            manifestDir = os.path.dirname(self.manifestPath)
            try:
                manifest["root"] = os.path.relpath(root, manifestDir)
            except ValueError:
                pass  # different drive on Windows: keep the absolute path
        self.manifest = manifest
        self.currentIndex = -1

    @staticmethod
    def validate(manifest):
        for case in manifest["cases"]:
            if "id" not in case or "image" not in case:
                raise ValueError("Each case needs at least an 'id' and an 'image' entry")
            case.setdefault("segmentation", None)
            case.setdefault("status", STATUS_PENDING)
            case.setdefault("comment", "")
            case.setdefault("reviewer", "")
            case.setdefault("reviewed_at", None)
            if case["status"] not in ALL_STATUSES:
                raise ValueError(f"Case '{case['id']}' has unknown status '{case['status']}'")

    def save(self, manifestPath=None):
        """Write the manifest atomically (write to a temporary file, then replace)."""
        if manifestPath:
            self.manifestPath = os.path.abspath(manifestPath)
        if self.manifest is None or not self.manifestPath:
            raise RuntimeError("No manifest to save")
        directory = os.path.dirname(self.manifestPath)
        os.makedirs(directory, exist_ok=True)
        fd, tmpPath = tempfile.mkstemp(prefix=".datareview-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.manifest, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmpPath, self.manifestPath)
        except Exception:
            if os.path.exists(tmpPath):
                os.remove(tmpPath)
            raise

    # ---- Case access -----------------------------------------------------------------------------

    @property
    def cases(self):
        return self.manifest["cases"] if self.manifest else []

    def caseCount(self):
        return len(self.cases)

    def case(self, index):
        return self.cases[index]

    def resolvePath(self, path):
        """Resolve a case path (absolute, or relative to the manifest root / manifest folder)."""
        if not path:
            return None
        if os.path.isabs(path):
            return path
        root = self.manifest.get("root") if self.manifest else None
        manifestDir = os.path.dirname(self.manifestPath) if self.manifestPath else os.getcwd()
        if root:
            if not os.path.isabs(root):
                root = os.path.join(manifestDir, root)
            return os.path.normpath(os.path.join(root, path))
        return os.path.normpath(os.path.join(manifestDir, path))

    def summary(self):
        counts = {status: 0 for status in ALL_STATUSES}
        for case in self.cases:
            counts[case.get("status", STATUS_PENDING)] += 1
        counts["total"] = len(self.cases)
        counts["reviewed"] = counts[STATUS_ACCEPTED] + counts[STATUS_REJECTED]
        return counts

    def nextIndexWithStatus(self, fromIndex, status=STATUS_PENDING, wrap=True):
        """Index of the next case after ``fromIndex`` with the given status, or -1."""
        n = self.caseCount()
        if n == 0:
            return -1
        order = range(fromIndex + 1, n)
        if wrap:
            order = list(order) + list(range(0, fromIndex + 1))
        for i in order:
            if self.cases[i].get("status") == status:
                return i
        return -1

    # ---- Review decisions ------------------------------------------------------------------------

    def setDecision(self, index, status, reviewer="", comment=None, save=True):
        if status not in ALL_STATUSES:
            raise ValueError(f"Unknown status '{status}'")
        case = self.cases[index]
        case["status"] = status
        if comment is not None:
            case["comment"] = comment
        if status == STATUS_PENDING:
            case["reviewer"] = ""
            case["reviewed_at"] = None
        else:
            case["reviewer"] = reviewer or ""
            case["reviewed_at"] = _utcTimestamp()
        if save:
            self.save()

    def setComment(self, index, comment, save=True):
        case = self.cases[index]
        if case.get("comment", "") == comment:
            return False
        case["comment"] = comment
        if save:
            self.save()
        return True

    # ---- Scene handling --------------------------------------------------------------------------

    def unloadCurrentCase(self):
        for node in (self.imageNode, self.segmentationNode):
            if node is not None and slicer.mrmlScene.IsNodePresent(node):
                slicer.mrmlScene.RemoveNode(node)
        self.imageNode = None
        self.segmentationNode = None

    def loadCase(self, index, fitToViews=False, showSegmentation=True, fillOpacity=0.5):
        """Load the image and segmentation of a case into the scene, replacing the previous case.

        Returns (imageNode, segmentationNode); segmentationNode may be None.
        Raises on a missing image file; a missing segmentation file is logged as a warning.
        """
        case = self.cases[index]
        imagePath = self.resolvePath(case["image"])
        if not imagePath or not os.path.isfile(imagePath):
            raise FileNotFoundError(f"Image not found for case '{case['id']}': {imagePath}")

        self.unloadCurrentCase()

        self.imageNode = slicer.util.loadVolume(imagePath, {"name": case["id"], "show": False})

        segmentationPath = self.resolvePath(case.get("segmentation"))
        if segmentationPath:
            if os.path.isfile(segmentationPath):
                self.segmentationNode = slicer.util.loadSegmentation(segmentationPath, {"name": f"{case['id']} segmentation"})
                self.segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(self.imageNode)
                displayNode = self.segmentationNode.GetDisplayNode()
                if displayNode:
                    displayNode.SetVisibility(showSegmentation)
                    displayNode.SetOpacity2DFill(fillOpacity)
            else:
                logging.warning(f"DataReview: segmentation not found for case '{case['id']}': {segmentationPath}")

        slicer.util.setSliceViewerLayers(background=self.imageNode, foreground=None, label=None, fit=fitToViews)
        self.currentIndex = index
        return self.imageNode, self.segmentationNode

    def setSegmentationVisible(self, visible):
        if self.segmentationNode and self.segmentationNode.GetDisplayNode():
            self.segmentationNode.GetDisplayNode().SetVisibility(visible)

    def setSegmentationFillOpacity(self, opacity):
        if self.segmentationNode and self.segmentationNode.GetDisplayNode():
            self.segmentationNode.GetDisplayNode().SetOpacity2DFill(opacity)


#
# DataReviewWidget
#


class DataReviewWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
    """Uses ScriptedLoadableModuleWidget base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/scripts/slicer/ScriptedLoadableModule.py
    """

    SETTINGS_PREFIX = "DataReview/"

    STATUS_PREFIX = {STATUS_PENDING: "• ", STATUS_ACCEPTED: "✓ ", STATUS_REJECTED: "✗ "}

    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)
        self.logic = None
        self.shortcuts = []
        self._updatingGUI = False
        self._viewsFittedOnce = False

    def setup(self):
        ScriptedLoadableModuleWidget.setup(self)

        uiWidget = slicer.util.loadUI(self.resourcePath("UI/DataReview.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)
        uiWidget.setMRMLScene(slicer.mrmlScene)

        self.logic = DataReviewLogic()

        pathLineEdit = ctk.ctkPathLineEdit
        self.ui.manifestPathLineEdit.filters = pathLineEdit.Files
        self.ui.manifestPathLineEdit.nameFilters = ["Review manifest (*.json)"]
        self.ui.manifestPathLineEdit.settingKey = self.SETTINGS_PREFIX + "manifestPath"
        self.ui.rootDirectoryLineEdit.filters = pathLineEdit.Dirs
        self.ui.rootDirectoryLineEdit.settingKey = self.SETTINGS_PREFIX + "rootDirectory"

        settings = qt.QSettings()
        self.ui.reviewerLineEdit.text = settings.value(self.SETTINGS_PREFIX + "reviewer", "")
        self.ui.imagePatternLineEdit.text = settings.value(self.SETTINGS_PREFIX + "imagePattern", self.ui.imagePatternLineEdit.text)
        self.ui.segmentationPatternLineEdit.text = settings.value(self.SETTINGS_PREFIX + "segmentationPattern", self.ui.segmentationPatternLineEdit.text)

        # Connections
        self.ui.loadManifestButton.connect("clicked(bool)", self.onLoadManifest)
        self.ui.scanButton.connect("clicked(bool)", self.onScanDirectory)
        self.ui.reviewerLineEdit.connect("editingFinished()", self.onReviewerChanged)
        self.ui.caseListWidget.connect("currentRowChanged(int)", self.onCaseListRowChanged)
        self.ui.previousButton.connect("clicked(bool)", self.onPrevious)
        self.ui.nextButton.connect("clicked(bool)", self.onNext)
        self.ui.nextPendingButton.connect("clicked(bool)", self.onNextPending)
        self.ui.acceptButton.connect("clicked(bool)", lambda: self.onDecision(STATUS_ACCEPTED))
        self.ui.rejectButton.connect("clicked(bool)", lambda: self.onDecision(STATUS_REJECTED))
        self.ui.clearDecisionButton.connect("clicked(bool)", lambda: self.onDecision(STATUS_PENDING))
        self.ui.showSegmentationCheckBox.connect("toggled(bool)", self.logic.setSegmentationVisible)
        self.ui.opacitySliderWidget.connect("valueChanged(double)", self.logic.setSegmentationFillOpacity)
        self.ui.resetViewsButton.connect("clicked(bool)", self.onResetViews)

        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)

        self.updateGUI()

    def cleanup(self):
        self.removeObservers()
        self.removeShortcuts()

    def enter(self):
        self.installShortcuts()

    def exit(self):
        self.commitComment()
        self.removeShortcuts()

    def onSceneStartClose(self, caller, event):
        self.commitComment()
        self.logic.imageNode = None
        self.logic.segmentationNode = None
        self.logic.currentIndex = -1
        self._viewsFittedOnce = False
        self.updateGUI()

    # ---- Keyboard shortcuts ----------------------------------------------------------------------

    def installShortcuts(self):
        if self.shortcuts:
            return
        mainWindow = slicer.util.mainWindow()
        if mainWindow is None:
            return
        bindings = [
            ("A", lambda: self.onDecision(STATUS_ACCEPTED)),
            ("R", lambda: self.onDecision(STATUS_REJECTED)),
            ("N", self.onNext),
            ("Right", self.onNext),
            ("P", self.onPrevious),
            ("Left", self.onPrevious),
            ("U", self.onNextPending),
            ("S", lambda: self.ui.showSegmentationCheckBox.setChecked(not self.ui.showSegmentationCheckBox.checked)),
        ]
        for key, callback in bindings:
            shortcut = qt.QShortcut(mainWindow)
            shortcut.setKey(qt.QKeySequence(key))
            shortcut.connect("activated()", self._guardedShortcut(callback))
            self.shortcuts.append(shortcut)

    def _guardedShortcut(self, callback):
        """Ignore shortcuts while the user is typing in a text field."""
        def run():
            focus = qt.QApplication.focusWidget()
            if focus is not None and isinstance(focus, (qt.QLineEdit, qt.QPlainTextEdit, qt.QTextEdit, qt.QAbstractSpinBox)):
                return
            callback()
        return run

    def removeShortcuts(self):
        for shortcut in self.shortcuts:
            shortcut.setParent(None)
            shortcut.deleteLater()
        self.shortcuts = []

    # ---- Dataset / manifest ----------------------------------------------------------------------

    def onLoadManifest(self):
        path = self.ui.manifestPathLineEdit.currentPath
        if not path:
            slicer.util.errorDisplay(_("Select a manifest JSON file first."))
            return
        try:
            self.logic.load(path)
        except Exception as e:
            slicer.util.errorDisplay(_("Failed to load manifest: {error}").format(error=str(e)))
            return
        self.ui.manifestPathLineEdit.addCurrentPathToHistory()
        self.afterManifestLoaded()

    def onScanDirectory(self):
        rootDirectory = self.ui.rootDirectoryLineEdit.currentPath
        imagePattern = self.ui.imagePatternLineEdit.text.strip()
        segmentationPattern = self.ui.segmentationPatternLineEdit.text.strip() or None
        if not rootDirectory or not imagePattern:
            slicer.util.errorDisplay(_("Select a dataset folder and an image pattern first."))
            return

        settings = qt.QSettings()
        settings.setValue(self.SETTINGS_PREFIX + "imagePattern", imagePattern)
        settings.setValue(self.SETTINGS_PREFIX + "segmentationPattern", segmentationPattern or "")

        manifestPath = os.path.join(os.path.abspath(rootDirectory), "review_manifest.json")
        try:
            manifest = DataReviewLogic.scanDirectory(rootDirectory, imagePattern, segmentationPattern)
            previous = None
            if os.path.isfile(manifestPath):
                with open(manifestPath, encoding="utf-8") as f:
                    previous = json.load(f)
            manifest = DataReviewLogic.mergeReviewState(manifest, previous)
            self.logic.setManifest(manifest, manifestPath)
            self.logic.save()
        except Exception as e:
            slicer.util.errorDisplay(_("Failed to create manifest: {error}").format(error=str(e)))
            return

        self.ui.rootDirectoryLineEdit.addCurrentPathToHistory()
        self.ui.manifestPathLineEdit.currentPath = manifestPath
        self.ui.manifestPathLineEdit.addCurrentPathToHistory()
        self.ui.scanGroupBox.collapsed = True
        self.afterManifestLoaded()
        slicer.util.infoDisplay(
            _("Found {count} cases. Manifest written to:\n{path}").format(count=self.logic.caseCount(), path=manifestPath),
            windowTitle=_("Data Review"))

    def afterManifestLoaded(self):
        self._viewsFittedOnce = False
        self.populateCaseList()
        if self.logic.caseCount() == 0:
            self.updateGUI()
            return
        index = self.logic.nextIndexWithStatus(-1, STATUS_PENDING)
        self.showCase(index if index >= 0 else 0)

    def onReviewerChanged(self):
        qt.QSettings().setValue(self.SETTINGS_PREFIX + "reviewer", self.ui.reviewerLineEdit.text)

    # ---- Navigation ------------------------------------------------------------------------------

    def onCaseListRowChanged(self, row):
        if self._updatingGUI or row < 0 or row == self.logic.currentIndex:
            return
        self.showCase(row)

    def onPrevious(self):
        if self.logic.caseCount():
            self.showCase((self.logic.currentIndex - 1) % self.logic.caseCount())

    def onNext(self):
        if self.logic.caseCount():
            self.showCase((self.logic.currentIndex + 1) % self.logic.caseCount())

    def onNextPending(self):
        index = self.logic.nextIndexWithStatus(self.logic.currentIndex, STATUS_PENDING)
        if index < 0:
            slicer.util.infoDisplay(_("All cases have been reviewed."), windowTitle=_("Data Review"))
            return
        self.showCase(index)

    def showCase(self, index):
        if index < 0 or index >= self.logic.caseCount():
            return
        self.commitComment()
        fit = (not self.ui.keepSlicePositionsCheckBox.checked) or (not self._viewsFittedOnce)
        try:
            with slicer.util.tryWithErrorDisplay(_("Failed to load case."), waitCursor=True):
                self.logic.loadCase(index,
                                    fitToViews=fit,
                                    showSegmentation=self.ui.showSegmentationCheckBox.checked,
                                    fillOpacity=self.ui.opacitySliderWidget.value)
                self._viewsFittedOnce = True
        finally:
            self.updateGUI()

    def onResetViews(self):
        if self.logic.imageNode:
            slicer.util.setSliceViewerLayers(background=self.logic.imageNode, fit=True)

    # ---- Decisions and comments ------------------------------------------------------------------

    def commitComment(self):
        """Store the comment text of the current case if it was edited."""
        index = self.logic.currentIndex
        if self.logic.manifest is None or index < 0:
            return
        try:
            if self.logic.setComment(index, self.ui.commentTextEdit.plainText):
                self.updateCaseListItem(index)
        except Exception as e:
            logging.error(f"DataReview: failed to save comment: {e}")

    def onDecision(self, status):
        index = self.logic.currentIndex
        if self.logic.manifest is None or index < 0:
            return
        try:
            self.logic.setDecision(index, status,
                                   reviewer=self.ui.reviewerLineEdit.text.strip(),
                                   comment=self.ui.commentTextEdit.plainText)
        except Exception as e:
            slicer.util.errorDisplay(_("Failed to save decision: {error}").format(error=str(e)))
            return
        self.updateCaseListItem(index)
        if status != STATUS_PENDING and self.ui.advanceAfterDecisionCheckBox.checked:
            nextIndex = self.logic.nextIndexWithStatus(index, STATUS_PENDING)
            if nextIndex >= 0:
                self.showCase(nextIndex)
                return
        self.updateGUI()

    # ---- GUI updates -----------------------------------------------------------------------------

    def caseListText(self, case):
        text = self.STATUS_PREFIX[case.get("status", STATUS_PENDING)] + case["id"]
        if case.get("comment"):
            text += "  \U0001F4AC"
        return text

    def caseListColor(self, case):
        status = case.get("status", STATUS_PENDING)
        if status == STATUS_ACCEPTED:
            return qt.QColor(46, 139, 87)
        if status == STATUS_REJECTED:
            return qt.QColor(200, 60, 60)
        return None

    def populateCaseList(self):
        self._updatingGUI = True
        try:
            self.ui.caseListWidget.clear()
            for case in self.logic.cases:
                item = qt.QListWidgetItem(self.caseListText(case))
                color = self.caseListColor(case)
                if color:
                    item.setForeground(qt.QBrush(color))
                tooltip = case["image"]
                if case.get("segmentation"):
                    tooltip += "\n" + case["segmentation"]
                item.setToolTip(tooltip)
                self.ui.caseListWidget.addItem(item)
        finally:
            self._updatingGUI = False

    def updateCaseListItem(self, index):
        item = self.ui.caseListWidget.item(index)
        if item is None:
            return
        case = self.logic.case(index)
        item.setText(self.caseListText(case))
        color = self.caseListColor(case)
        item.setForeground(qt.QBrush(color) if color else qt.QBrush())

    def updateGUI(self):
        self._updatingGUI = True
        try:
            hasManifest = self.logic.manifest is not None
            hasCase = hasManifest and self.logic.currentIndex >= 0
            for widget in (self.ui.previousButton, self.ui.nextButton, self.ui.nextPendingButton, self.ui.caseListWidget):
                widget.enabled = hasManifest and self.logic.caseCount() > 0
            for widget in (self.ui.acceptButton, self.ui.rejectButton, self.ui.clearDecisionButton, self.ui.commentTextEdit):
                widget.enabled = hasCase

            if not hasManifest:
                self.ui.progressLabel.text = _("No manifest loaded.")
                self.ui.progressBar.value = 0
                self.ui.caseInfoLabel.text = "-"
                self.ui.commentTextEdit.plainText = ""
                return

            counts = self.logic.summary()
            self.ui.progressLabel.text = _("{reviewed} of {total} reviewed  ({accepted} accepted, {rejected} rejected, {pending} pending)").format(
                reviewed=counts["reviewed"], total=counts["total"], accepted=counts[STATUS_ACCEPTED],
                rejected=counts[STATUS_REJECTED], pending=counts[STATUS_PENDING])
            self.ui.progressBar.maximum = max(counts["total"], 1)
            self.ui.progressBar.value = counts["reviewed"]

            if hasCase:
                index = self.logic.currentIndex
                case = self.logic.case(index)
                self.ui.caseListWidget.setCurrentRow(index)
                status = case.get("status", STATUS_PENDING)
                lines = [f"<b>{case['id']}</b>  ({index + 1}/{self.logic.caseCount()})",
                         _("Status: <b>{status}</b>").format(status=status)]
                if case.get("reviewer") or case.get("reviewed_at"):
                    lines.append(_("Reviewed by {reviewer} at {time}").format(
                        reviewer=case.get("reviewer") or "?", time=case.get("reviewed_at") or "?"))
                lines.append(_("Image: {path}").format(path=case["image"]))
                lines.append(_("Segmentation: {path}").format(path=case.get("segmentation") or _("(none)")))
                self.ui.caseInfoLabel.text = "<br>".join(lines)
                self.ui.commentTextEdit.plainText = case.get("comment", "")
            else:
                self.ui.caseInfoLabel.text = "-"
                self.ui.commentTextEdit.plainText = ""
        finally:
            self._updatingGUI = False


#
# DataReviewTest
#


class DataReviewTest(ScriptedLoadableModuleTest):
    """Tests the manifest logic on a small synthetic dataset and loads a case into the scene."""

    def setUp(self):
        slicer.mrmlScene.Clear()

    def runTest(self):
        self.setUp()
        self.test_DataReview1()

    def _writeSyntheticDataset(self, root, caseIds):
        import numpy as np
        os.makedirs(os.path.join(root, "images"))
        os.makedirs(os.path.join(root, "labels"))
        for i, caseId in enumerate(caseIds):
            shape = (12, 16, 20)
            image = np.random.default_rng(i).normal(100, 20, shape).astype("float32")
            label = np.zeros(shape, dtype="uint8")
            label[4:8, 5:10, 6:12] = 1
            imageNode = slicer.util.addVolumeFromArray(image, name="tmpImage")
            labelNode = slicer.util.addVolumeFromArray(label, name="tmpLabel", nodeClassName="vtkMRMLLabelMapVolumeNode")
            self.assertTrue(slicer.util.saveNode(imageNode, os.path.join(root, "images", f"{caseId}.nii.gz")))
            self.assertTrue(slicer.util.saveNode(labelNode, os.path.join(root, "labels", f"{caseId}_seg.nii.gz")))
            slicer.mrmlScene.RemoveNode(imageNode)
            slicer.mrmlScene.RemoveNode(labelNode)

    def test_DataReview1(self):
        self.delayDisplay("Creating synthetic dataset")
        root = slicer.util.tempDirectory("DataReviewTest")
        caseIds = ["sub-001", "sub-002", "sub-003"]
        self._writeSyntheticDataset(root, caseIds)

        self.delayDisplay("Scanning folder")
        manifest = DataReviewLogic.scanDirectory(root, "images/*.nii.gz", "labels/*.nii.gz")
        self.assertEqual([c["id"] for c in manifest["cases"]], caseIds)
        for case in manifest["cases"]:
            self.assertIsNotNone(case["segmentation"], f"segmentation not paired for {case['id']}")
            self.assertEqual(case["status"], STATUS_PENDING)

        manifestPath = os.path.join(root, "review_manifest.json")
        logic = DataReviewLogic()
        logic.setManifest(manifest, manifestPath)
        logic.save()
        self.assertTrue(os.path.isfile(manifestPath))
        self.assertEqual(logic.manifest["root"], ".", "root should be stored relative to the manifest")
        self.assertEqual(logic.resolvePath(logic.case(0)["image"]), os.path.join(root, "images", "sub-001.nii.gz"))

        self.delayDisplay("Loading a case into the scene")
        imageNode, segmentationNode = logic.loadCase(0, fitToViews=True)
        self.assertIsNotNone(imageNode)
        self.assertIsNotNone(segmentationNode)
        self.assertEqual(segmentationNode.GetSegmentation().GetNumberOfSegments(), 1)
        self.assertEqual(logic.currentIndex, 0)

        logic.loadCase(1)
        self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")), 1, "previous case should be removed")
        self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLSegmentationNode")), 1)

        self.delayDisplay("Recording decisions")
        logic.setDecision(0, STATUS_ACCEPTED, reviewer="tester")
        logic.setDecision(1, STATUS_REJECTED, reviewer="tester", comment="lesion mask too large")
        logic.setComment(2, "needs a second look")
        counts = logic.summary()
        self.assertEqual(counts["reviewed"], 2)
        self.assertEqual(counts[STATUS_PENDING], 1)
        self.assertEqual(logic.nextIndexWithStatus(-1, STATUS_PENDING), 2)
        self.assertEqual(logic.nextIndexWithStatus(2, STATUS_PENDING), 2)  # wraps around to itself
        self.assertEqual(logic.nextIndexWithStatus(2, STATUS_PENDING, wrap=False), -1)

        self.delayDisplay("Reloading the manifest from disk")
        logic2 = DataReviewLogic()
        logic2.load(manifestPath)
        self.assertEqual(logic2.case(0)["status"], STATUS_ACCEPTED)
        self.assertEqual(logic2.case(0)["reviewer"], "tester")
        self.assertIsNotNone(logic2.case(0)["reviewed_at"])
        self.assertEqual(logic2.case(1)["comment"], "lesion mask too large")
        self.assertEqual(logic2.case(2)["comment"], "needs a second look")
        self.assertEqual(logic2.case(2)["status"], STATUS_PENDING)

        self.delayDisplay("Rescanning preserves review state")
        rescanned = DataReviewLogic.mergeReviewState(
            DataReviewLogic.scanDirectory(root, "images/*.nii.gz", "labels/*.nii.gz"), logic2.manifest)
        self.assertEqual(rescanned["cases"][1]["status"], STATUS_REJECTED)

        logic.setDecision(0, STATUS_PENDING)
        self.assertEqual(logic.case(0)["reviewer"], "")

        logic.unloadCurrentCase()
        self.delayDisplay("Test passed")
