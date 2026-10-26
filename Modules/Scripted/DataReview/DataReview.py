import datetime
import glob
import json
import logging
import os
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
<p>Step through a dataset of diffusion images (ADC and trace-weighted) with their stroke lesion
segmentations, and accept or reject each case with a comment.</p>

<p>The list of cases and the review state (status, comment, reviewer, timestamp) live in a single
JSON <i>manifest</i> file that is saved automatically after every change. The ADC and TRACEW images are
loaded into every view as background and foreground layers: the opacity slider in the view controller
(or the blend slider in the panel, or the <b>T</b> key) switches between them. The three views are fixed to
axial, sagittal and coronal and are linked, and the segmentation is overlaid on all of them.</p>

<p>Keyboard shortcuts (active while this module is open): <b>A</b> accept, <b>R</b> reject,
<b>N</b>/<b>Right</b> next, <b>P</b>/<b>Left</b> previous, <b>U</b> next unreviewed,
<b>S</b> cycle the lesion display (filled, outline only, hidden), <b>T</b> switch between ADC and TRACEW,
<b>Ctrl+Shift+U</b> toggle the simplified interface.</p>
""")
        self.parent.acknowledgementText = _("""
Developed for reviewing imaging datasets and their stroke lesion segmentations.
""")


#
# Constants
#

STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"
ALL_STATUSES = (STATUS_PENDING, STATUS_ACCEPTED, STATUS_REJECTED)

MANIFEST_FORMAT = "SlicerDataReview"
MANIFEST_VERSION = 2

DEFAULT_CHANNELS = ["ADC", "TRACEW"]

# Layout id for the custom review layout (must not collide with built-in layouts).
REVIEW_LAYOUT_ID = 1504
# Slice views used for review and the orientation each one is locked to.
REVIEW_VIEWS = {"Red": "Axial", "Yellow": "Sagittal", "Green": "Coronal"}

# Suffixes appended to file names that identify a channel or a segmentation; stripped before pairing.
PAIRING_SUFFIXES = ("_adc", "-adc", "_tracew", "-tracew", "_trace", "-trace", "_dwi", "-dwi", "_b1000", "-b1000",
                    "_seg", "-seg", "_segmentation", "_label", "_labels", "_mask", "_lesion", "_gt", "_image", "_img")
# File stems that carry no case information at all (e.g. sub-001/adc.nii.gz); the folder name is used instead.
GENERIC_STEMS = {"adc", "tracew", "trace", "dwi", "b1000", "seg", "segmentation", "label", "labels", "mask",
                 "lesion", "gt", "image", "img", ""}

# Lesion display modes, in the order the S key cycles through them.
LESION_FILLED = "filled"
LESION_OUTLINE = "outline"
LESION_HIDDEN = "hidden"
LESION_MODES = (LESION_FILLED, LESION_OUTLINE, LESION_HIDDEN)

SETTINGS_PREFIX = "DataReview/"
SETTING_AUTOLOAD = SETTINGS_PREFIX + "autoLoadManifest"
SETTING_SIMPLIFIED_UI = SETTINGS_PREFIX + "simplifiedUI"
SETTING_REVIEWER = SETTINGS_PREFIX + "reviewer"


def _stripImageExtensions(filename):
    """Return the base name of an image file without its (possibly double) extension."""
    name = os.path.basename(filename)
    for ext in (".seg.nrrd", ".nii.gz", ".img.gz", ".nrrd", ".nhdr", ".nii", ".mha", ".mhd", ".img", ".hdr"):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return os.path.splitext(name)[0]


def _caseKey(path, rootDirectory):
    """Derive the case id that a file belongs to from its name (or its folder for generic names)."""
    stem = _stripImageExtensions(path)
    low = stem.lower()
    for suffix in PAIRING_SUFFIXES:
        if low.endswith(suffix):
            stem = stem[: -len(suffix)]
            low = stem.lower()
            break
    if low in GENERIC_STEMS:
        parent = os.path.dirname(os.path.abspath(path))
        if parent != rootDirectory:
            return os.path.basename(parent)
    return stem


def _utcTimestamp():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _settingBool(key, default=False):
    value = qt.QSettings().value(key, default)
    if isinstance(value, str):
        return value.lower() in ("1", "true", "yes")
    return bool(value)


#
# DataReviewLogic
#


class DataReviewLogic(ScriptedLoadableModuleLogic):
    """Manages the review manifest (JSON) and loads cases into the scene.

    The manifest is a dictionary of the form::

        {
          "format": "SlicerDataReview",
          "version": 2,
          "root": ".",                              # base for relative case paths (relative to the manifest)
          "channels": ["ADC", "TRACEW"],            # image channels, in display order
          "cases": [
            {
              "id": "sub-001",
              "images": {"ADC": "adc/sub-001.nii.gz", "TRACEW": "tracew/sub-001.nii.gz"},
              "segmentation": "labels/sub-001.nii.gz",   # may be null
              "status": "pending" | "accepted" | "rejected",
              "comment": "",
              "reviewer": "",
              "reviewed_at": null | ISO-8601 timestamp
            }
          ]
        }

    Version 1 manifests with a single ``"image"`` entry per case are upgraded on load.
    """

    def __init__(self):
        ScriptedLoadableModuleLogic.__init__(self)
        self.manifestPath = None
        self.manifest = None
        self.currentIndex = -1
        self.imageNodes = {}  # channel -> volume node
        self.segmentationNode = None

    # ---- Manifest creation / IO ----------------------------------------------------------------

    @staticmethod
    def emptyManifest(root=None, channels=None):
        return {"format": MANIFEST_FORMAT, "version": MANIFEST_VERSION, "root": root,
                "channels": list(channels or DEFAULT_CHANNELS), "cases": []}

    @staticmethod
    def scanDirectory(rootDirectory, imagePatterns, segmentationPattern=None):
        """Build a manifest by matching image channels and segmentations by case key.

        :param rootDirectory: dataset folder; patterns are evaluated relative to it.
        :param imagePatterns: dict channel name -> glob pattern (e.g. ``{"ADC": "adc/*.nii.gz"}``),
            or a single pattern string for a one-channel dataset.
        :param segmentationPattern: glob pattern for the segmentations (optional).
        :return: manifest dictionary (not saved). Case paths are stored relative to ``rootDirectory``.
        """
        rootDirectory = os.path.abspath(rootDirectory)
        if not os.path.isdir(rootDirectory):
            raise ValueError(f"Dataset folder does not exist: {rootDirectory}")
        if isinstance(imagePatterns, str):
            imagePatterns = {"Image": imagePatterns}
        imagePatterns = {ch: pat for ch, pat in imagePatterns.items() if pat}
        if not imagePatterns:
            raise ValueError("At least one image pattern is required")

        def findFiles(pattern):
            files = sorted(glob.glob(os.path.join(rootDirectory, pattern), recursive=True))
            return [f for f in files if os.path.isfile(f)]

        filesByKey = {}  # key -> {"images": {channel: file}, "segmentation": file}
        for channel, pattern in imagePatterns.items():
            files = findFiles(pattern)
            if not files:
                raise ValueError(f"No {channel} images found for pattern '{pattern}' in {rootDirectory}")
            for f in files:
                key = _caseKey(f, rootDirectory)
                entry = filesByKey.setdefault(key, {"images": {}, "segmentation": None})
                if channel in entry["images"]:
                    logging.warning(f"DataReview: several {channel} files map to case '{key}', keeping {entry['images'][channel]}")
                    continue
                entry["images"][channel] = f
        if segmentationPattern:
            for f in findFiles(segmentationPattern):
                key = _caseKey(f, rootDirectory)
                entry = filesByKey.get(key)
                if entry is None:
                    logging.warning(f"DataReview: segmentation {f} does not match any image, skipped")
                    continue
                if entry["segmentation"] is None:
                    entry["segmentation"] = f

        manifest = DataReviewLogic.emptyManifest(rootDirectory, list(imagePatterns.keys()))
        for key in sorted(filesByKey):
            entry = filesByKey[key]
            manifest["cases"].append({
                "id": key,
                "images": {ch: (os.path.relpath(entry["images"][ch], rootDirectory) if ch in entry["images"] else None)
                           for ch in imagePatterns},
                "segmentation": os.path.relpath(entry["segmentation"], rootDirectory) if entry["segmentation"] else None,
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
        """Check the manifest and fill in defaults (also upgrades version 1 manifests in place)."""
        cases = manifest.get("cases")
        if not isinstance(cases, list):
            raise ValueError("Manifest has no 'cases' list")
        for case in cases:
            if "id" not in case:
                raise ValueError("Each case needs an 'id' entry")
            if "images" not in case:
                if "image" in case:
                    case["images"] = {"Image": case.pop("image")}
                else:
                    raise ValueError(f"Case '{case['id']}' has no 'images' entry")
            if not isinstance(case["images"], dict) or not case["images"]:
                raise ValueError(f"Case '{case['id']}' needs an 'images' dictionary (channel -> path)")
            case.setdefault("segmentation", None)
            case.setdefault("status", STATUS_PENDING)
            case.setdefault("comment", "")
            case.setdefault("reviewer", "")
            case.setdefault("reviewed_at", None)
            if case["status"] not in ALL_STATUSES:
                raise ValueError(f"Case '{case['id']}' has unknown status '{case['status']}'")
        if not manifest.get("channels"):
            manifest["channels"] = list(cases[0]["images"].keys()) if cases else list(DEFAULT_CHANNELS)
        manifest["version"] = MANIFEST_VERSION
        manifest.setdefault("format", MANIFEST_FORMAT)

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

    @property
    def channels(self):
        return self.manifest.get("channels", []) if self.manifest else []

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
        if comment is None:
            comment = self.cases[index].get("comment", "")
        if status == STATUS_REJECTED and not comment.strip():
            raise ValueError("A comment describing the reason is required to reject a case")
        case = self.cases[index]
        case["status"] = status
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

    # ---- Layout ----------------------------------------------------------------------------------

    @staticmethod
    def reviewLayoutXml():
        """Large axial view on the left, sagittal and coronal stacked on the right."""
        def view(name, orientation, color):
            return f"""
    <item>
     <view class="vtkMRMLSliceNode" singletontag="{name}">
      <property name="orientation" action="default">{orientation}</property>
      <property name="viewlabel" action="default">{name[0]}</property>
      <property name="viewcolor" action="default">{color}</property>
     </view>
    </item>"""
        return (
            '<layout type="horizontal" split="true">'
            + view("Red", "Axial", "#F34A33")
            + '\n   <item>\n    <layout type="vertical">'
            + view("Yellow", "Sagittal", "#EDD54C")
            + view("Green", "Coronal", "#6EB04B")
            + "\n    </layout>\n   </item>\n  </layout>"
        )

    @staticmethod
    def applyLayout():
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return
        layoutNode = layoutManager.layoutLogic().GetLayoutNode()
        if not layoutNode.IsLayoutDescription(REVIEW_LAYOUT_ID):
            layoutNode.AddLayoutDescription(REVIEW_LAYOUT_ID, DataReviewLogic.reviewLayoutXml())
        if layoutManager.layout != REVIEW_LAYOUT_ID:
            layoutManager.setLayout(REVIEW_LAYOUT_ID)

    # ---- Scene handling --------------------------------------------------------------------------

    def unloadCurrentCase(self):
        for node in list(self.imageNodes.values()) + [self.segmentationNode]:
            if node is not None and slicer.mrmlScene.IsNodePresent(node):
                slicer.mrmlScene.RemoveNode(node)
        self.imageNodes = {}
        self.segmentationNode = None

    def loadCase(self, index, fitToViews=False, lesionMode=LESION_FILLED, fillOpacity=0.5, foregroundOpacity=0.0):
        """Load all image channels and the segmentation of a case, replacing the previous case.

        The first channel becomes the background and the second the foreground layer of every slice
        view, so the view controller opacity slider switches between them; the segmentation is overlaid
        on all views.

        Returns (imageNodes dict, segmentationNode); segmentationNode may be None.
        Raises if no image file of the case exists; missing single files are logged as warnings.
        """
        case = self.cases[index]
        self.unloadCurrentCase()

        channels = [ch for ch in self.channels if ch in case["images"]] or list(case["images"].keys())
        for channel in channels:
            imagePath = self.resolvePath(case["images"].get(channel))
            if not imagePath or not os.path.isfile(imagePath):
                logging.warning(f"DataReview: {channel} image not found for case '{case['id']}': {imagePath}")
                continue
            self.imageNodes[channel] = slicer.util.loadVolume(imagePath, {"name": f"{case['id']} {channel}", "show": False})
        if not self.imageNodes:
            raise FileNotFoundError(f"No image files found for case '{case['id']}'")
        referenceNode = next(iter(self.imageNodes.values()))

        segmentationPath = self.resolvePath(case.get("segmentation"))
        if segmentationPath:
            if os.path.isfile(segmentationPath):
                self.segmentationNode = slicer.util.loadSegmentation(segmentationPath, {"name": f"{case['id']} segmentation"})
                self.segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(referenceNode)
                displayNode = self.segmentationNode.GetDisplayNode()
                if displayNode:
                    displayNode.SetOpacity2DFill(fillOpacity)
                self.setLesionMode(lesionMode)
            else:
                logging.warning(f"DataReview: segmentation not found for case '{case['id']}': {segmentationPath}")

        self.showInViews(channels, fitToViews, foregroundOpacity)
        self.currentIndex = index
        return self.imageNodes, self.segmentationNode

    def showInViews(self, channels, fitToViews, foregroundOpacity=None):
        """Put the first channel in the background and the second in the foreground layer of all views."""
        self.applyLayout()
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return
        loaded = [ch for ch in channels if ch in self.imageNodes]
        backgroundNode = self.imageNodes[loaded[0]] if loaded else None
        foregroundNode = self.imageNodes[loaded[1]] if len(loaded) > 1 else None
        for viewName, orientation in REVIEW_VIEWS.items():
            sliceWidget = layoutManager.sliceWidget(viewName)
            if sliceWidget is None:
                continue
            sliceNode = sliceWidget.mrmlSliceNode()
            sliceNode.SetOrientation(orientation)
            compositeNode = sliceWidget.mrmlSliceCompositeNode()
            compositeNode.SetBackgroundVolumeID(backgroundNode.GetID() if backgroundNode else None)
            compositeNode.SetForegroundVolumeID(foregroundNode.GetID() if foregroundNode else None)
            compositeNode.SetLabelVolumeID(None)
            if foregroundOpacity is not None:
                compositeNode.SetForegroundOpacity(foregroundOpacity)
            # Lock the views together: scrolling, zooming or changing layers/opacity in one view
            # is applied to the others as well.
            compositeNode.SetLinkedControl(True)
            compositeNode.SetHotLinkedControl(True)
        if fitToViews:
            for viewName in REVIEW_VIEWS:
                sliceWidget = layoutManager.sliceWidget(viewName)
                if sliceWidget is not None:
                    sliceWidget.sliceLogic().FitSliceToAll()

    def setForegroundOpacity(self, opacity):
        """0 shows only the first channel (background), 1 only the second channel (foreground)."""
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return
        for viewName in REVIEW_VIEWS:
            sliceWidget = layoutManager.sliceWidget(viewName)
            if sliceWidget is not None:
                sliceWidget.mrmlSliceCompositeNode().SetForegroundOpacity(opacity)

    def foregroundOpacity(self):
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return 0.0
        sliceWidget = layoutManager.sliceWidget("Red")
        return sliceWidget.mrmlSliceCompositeNode().GetForegroundOpacity() if sliceWidget else 0.0

    def setLesionMode(self, mode):
        """Show the lesion filled, as an outline only, or not at all."""
        if mode not in LESION_MODES:
            raise ValueError(f"Unknown lesion display mode '{mode}'")
        if not self.segmentationNode or not self.segmentationNode.GetDisplayNode():
            return
        displayNode = self.segmentationNode.GetDisplayNode()
        displayNode.SetVisibility(mode != LESION_HIDDEN)
        displayNode.SetVisibility2DFill(mode == LESION_FILLED)
        displayNode.SetVisibility2DOutline(mode != LESION_HIDDEN)
        if mode == LESION_OUTLINE:
            displayNode.SetOpacity2DOutline(1.0)

    def lesionMode(self):
        if not self.segmentationNode or not self.segmentationNode.GetDisplayNode():
            return None
        displayNode = self.segmentationNode.GetDisplayNode()
        if not displayNode.GetVisibility():
            return LESION_HIDDEN
        return LESION_FILLED if displayNode.GetVisibility2DFill() else LESION_OUTLINE


#
# DataReviewWidget
#


class _MainWindowCloseFilter(qt.QObject):
    """Restores the full interface right before the main window closes.

    Slicer saves the main window state (including which toolbars are hidden) when it closes and
    restores it at the next start. Without this, a session that ran with the simplified interface
    would leave the next regular Slicer session without any toolbars or module selector.
    """

    def __init__(self, widget, parent=None):
        qt.QObject.__init__(self, parent)
        self.widget = widget

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Close and self.widget._simplifiedUIActive:
            self.widget.setSimplifiedUI(False)
            # If the close gets cancelled (e.g. "do you want to exit?"), re-apply the simplified interface.
            qt.QTimer.singleShot(0, self.widget._reapplySimplifiedUIIfStillOpen)
        return False


class DataReviewWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
    """Uses ScriptedLoadableModuleWidget base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    STATUS_PREFIX = {STATUS_PENDING: "• ", STATUS_ACCEPTED: "✓ ", STATUS_REJECTED: "✗ "}

    ACCEPT_STYLE = ("QPushButton { background-color: #2e8b57; color: white; font-weight: bold; font-size: 16px; "
                    "padding: 10px; border: 1px solid #1f6b40; border-radius: 5px; }"
                    "QPushButton:hover { background-color: #37a267; } QPushButton:pressed { background-color: #24703f; }"
                    "QPushButton:disabled { background-color: #a9c9b5; color: #eeeeee; border-color: #a9c9b5; }")
    REJECT_STYLE = ("QPushButton { background-color: #c83c3c; color: white; font-weight: bold; font-size: 16px; "
                    "padding: 10px; border: 1px solid #962a2a; border-radius: 5px; }"
                    "QPushButton:hover { background-color: #d94a4a; } QPushButton:pressed { background-color: #a32f2f; }"
                    "QPushButton:disabled { background-color: #dca5a5; color: #eeeeee; border-color: #dca5a5; }")

    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)
        self.logic = None
        self.shortcuts = []
        self._updatingGUI = False
        self._viewsFittedOnce = False
        self._autoLoadAttempted = False
        self._simplifiedUIActive = False
        self._closeFilter = None

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
        self.ui.manifestPathLineEdit.settingKey = SETTINGS_PREFIX + "manifestPath"
        self.ui.rootDirectoryLineEdit.filters = pathLineEdit.Dirs
        self.ui.rootDirectoryLineEdit.settingKey = SETTINGS_PREFIX + "rootDirectory"

        self.ui.acceptButton.setStyleSheet(self.ACCEPT_STYLE)
        self.ui.rejectButton.setStyleSheet(self.REJECT_STYLE)
        self.ui.rejectReasonLabel.setStyleSheet("QLabel { color: #c83c3c; font-weight: bold; }")
        self.ui.rejectReasonLabel.hide()

        settings = qt.QSettings()
        self.ui.reviewerLineEdit.text = settings.value(SETTING_REVIEWER, "")
        self.ui.adcPatternLineEdit.text = settings.value(SETTINGS_PREFIX + "adcPattern", self.ui.adcPatternLineEdit.text)
        self.ui.tracewPatternLineEdit.text = settings.value(SETTINGS_PREFIX + "tracewPattern", self.ui.tracewPatternLineEdit.text)
        self.ui.segmentationPatternLineEdit.text = settings.value(SETTINGS_PREFIX + "segmentationPattern", self.ui.segmentationPatternLineEdit.text)
        autoLoadPath = settings.value(SETTING_AUTOLOAD, "")
        self.ui.autoLoadCheckBox.checked = bool(autoLoadPath)
        if autoLoadPath and not self.ui.manifestPathLineEdit.currentPath:
            self.ui.manifestPathLineEdit.currentPath = autoLoadPath
        self.ui.simplifiedUICheckBox.checked = _settingBool(SETTING_SIMPLIFIED_UI)

        # Connections
        self.ui.loadManifestButton.connect("clicked(bool)", self.onLoadManifest)
        self.ui.scanButton.connect("clicked(bool)", self.onScanDirectory)
        self.ui.reviewerLineEdit.connect("editingFinished()", self.onReviewerChanged)
        self.ui.autoLoadCheckBox.connect("toggled(bool)", self.onAutoLoadToggled)
        self.ui.simplifiedUICheckBox.connect("toggled(bool)", self.onSimplifiedUIToggled)
        self.ui.caseListWidget.connect("currentRowChanged(int)", self.onCaseListRowChanged)
        self.ui.previousButton.connect("clicked(bool)", self.onPrevious)
        self.ui.nextButton.connect("clicked(bool)", self.onNext)
        self.ui.nextPendingButton.connect("clicked(bool)", self.onNextPending)
        self.ui.acceptButton.connect("clicked(bool)", lambda: self.onDecision(STATUS_ACCEPTED))
        self.ui.rejectButton.connect("clicked(bool)", lambda: self.onDecision(STATUS_REJECTED))
        self.ui.removeDecisionButton.connect("clicked(bool)", lambda: self.onDecision(STATUS_PENDING))
        self.ui.commentTextEdit.connect("textChanged()", self.onCommentTextChanged)
        for mode, label in ((LESION_FILLED, _("Filled")), (LESION_OUTLINE, _("Outline only")), (LESION_HIDDEN, _("Hidden"))):
            self.ui.lesionModeComboBox.addItem(label, mode)
        self.ui.lesionModeComboBox.connect("currentIndexChanged(int)", self.onLesionModeChanged)
        self.ui.blendSliderWidget.connect("valueChanged(double)", self.onBlendChanged)
        self.ui.resetViewsButton.connect("clicked(bool)", self.onResetViews)

        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)

        mainWindow = slicer.util.mainWindow()
        if mainWindow is not None:
            self._closeFilter = _MainWindowCloseFilter(self, mainWindow)
            mainWindow.installEventFilter(self._closeFilter)

        self.updateGUI()

    def cleanup(self):
        self.removeObservers()
        self.removeShortcuts()
        mainWindow = slicer.util.mainWindow()
        if self._closeFilter is not None and mainWindow is not None:
            mainWindow.removeEventFilter(self._closeFilter)
        self._closeFilter = None

    def enter(self):
        self.installShortcuts()
        if not self._autoLoadAttempted:
            self._autoLoadAttempted = True
            if _settingBool(SETTING_SIMPLIFIED_UI):
                self.setSimplifiedUI(True)
            autoLoadPath = qt.QSettings().value(SETTING_AUTOLOAD, "")
            if autoLoadPath and self.logic.manifest is None:
                if os.path.isfile(autoLoadPath):
                    self.ui.manifestPathLineEdit.currentPath = autoLoadPath
                    # Defer until the main window is fully shown so the views are ready.
                    qt.QTimer.singleShot(0, self.onLoadManifest)
                else:
                    logging.warning(f"DataReview: manifest configured for automatic loading does not exist: {autoLoadPath}")

    def exit(self):
        self.commitComment()
        self.removeShortcuts()

    def onSceneStartClose(self, caller, event):
        self.commitComment()
        self.logic.imageNodes = {}
        self.logic.segmentationNode = None
        self.logic.currentIndex = -1
        self._viewsFittedOnce = False
        self.updateGUI()

    # ---- Simplified ("kiosk") interface --------------------------------------------------------

    def setSimplifiedUI(self, simplified):
        """Hide everything a reviewer does not need: menus, toolbars, logo, help, status bar, Data Probe."""
        if slicer.util.mainWindow() is None:
            return
        visible = not simplified
        slicer.util.setToolbarsVisible(visible)
        slicer.util.setMenuBarsVisible(visible)
        slicer.util.setApplicationLogoVisible(visible)
        slicer.util.setModuleHelpSectionVisible(visible)
        slicer.util.setModulePanelTitleVisible(visible)
        slicer.util.setDataProbeVisible(visible)
        slicer.util.setStatusBarVisible(visible)
        if simplified:
            slicer.util.setPythonConsoleVisible(False)
            self.ui.datasetCollapsibleButton.collapsed = True
        self._simplifiedUIActive = simplified

    def toggleSimplifiedUI(self):
        self.setSimplifiedUI(not self._simplifiedUIActive)

    def _reapplySimplifiedUIIfStillOpen(self):
        mainWindow = slicer.util.mainWindow()
        if mainWindow is not None and mainWindow.isVisible() and _settingBool(SETTING_SIMPLIFIED_UI):
            self.setSimplifiedUI(True)

    def onSimplifiedUIToggled(self, checked):
        qt.QSettings().setValue(SETTING_SIMPLIFIED_UI, checked)

    def onAutoLoadToggled(self, checked):
        path = self.ui.manifestPathLineEdit.currentPath if checked else ""
        if checked and not path:
            self._updatingGUI = True
            self.ui.autoLoadCheckBox.checked = False
            self._updatingGUI = False
            slicer.util.errorDisplay(_("Select or create a manifest first."))
            return
        qt.QSettings().setValue(SETTING_AUTOLOAD, path)

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
            ("S", self.cycleLesionMode),
            ("T", self.toggleChannel),
        ]
        for key, callback in bindings:
            shortcut = qt.QShortcut(mainWindow)
            shortcut.setKey(qt.QKeySequence(key))
            shortcut.connect("activated()", self._guardedShortcut(callback))
            self.shortcuts.append(shortcut)
        # Escape hatch from the simplified interface for the person who sets the machine up.
        shortcut = qt.QShortcut(mainWindow)
        shortcut.setKey(qt.QKeySequence("Ctrl+Shift+U"))
        shortcut.connect("activated()", self.toggleSimplifiedUI)
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
        if self.ui.autoLoadCheckBox.checked:
            qt.QSettings().setValue(SETTING_AUTOLOAD, self.logic.manifestPath)
        self.afterManifestLoaded()

    def onScanDirectory(self):
        rootDirectory = self.ui.rootDirectoryLineEdit.currentPath
        patterns = {
            "ADC": self.ui.adcPatternLineEdit.text.strip(),
            "TRACEW": self.ui.tracewPatternLineEdit.text.strip(),
        }
        segmentationPattern = self.ui.segmentationPatternLineEdit.text.strip() or None
        if not rootDirectory or not any(patterns.values()):
            slicer.util.errorDisplay(_("Select a dataset folder and at least one image pattern first."))
            return

        settings = qt.QSettings()
        settings.setValue(SETTINGS_PREFIX + "adcPattern", patterns["ADC"])
        settings.setValue(SETTINGS_PREFIX + "tracewPattern", patterns["TRACEW"])
        settings.setValue(SETTINGS_PREFIX + "segmentationPattern", segmentationPattern or "")

        manifestPath = os.path.join(os.path.abspath(rootDirectory), "review_manifest.json")
        try:
            manifest = DataReviewLogic.scanDirectory(rootDirectory, patterns, segmentationPattern)
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
        if self.ui.autoLoadCheckBox.checked:
            qt.QSettings().setValue(SETTING_AUTOLOAD, manifestPath)
        self.ui.scanGroupBox.collapsed = True
        self.afterManifestLoaded()
        missing = sum(1 for c in self.logic.cases if not c.get("segmentation"))
        message = _("Found {count} cases. Manifest written to:\n{path}").format(count=self.logic.caseCount(), path=manifestPath)
        if missing:
            message += "\n" + _("{count} cases have no matching segmentation.").format(count=missing)
        slicer.util.infoDisplay(message, windowTitle=_("Data Review"))

    def afterManifestLoaded(self):
        self._viewsFittedOnce = False
        self.updateBlendLabels()
        self.populateCaseList()
        if self.logic.caseCount() == 0:
            self.updateGUI()
            return
        index = self.logic.nextIndexWithStatus(-1, STATUS_PENDING)
        self.showCase(index if index >= 0 else 0)

    def onReviewerChanged(self):
        qt.QSettings().setValue(SETTING_REVIEWER, self.ui.reviewerLineEdit.text)

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
                self.logic.loadCase(index, fitToViews=fit, lesionMode=self.currentLesionMode(),
                                    foregroundOpacity=self.ui.blendSliderWidget.value)
                self._viewsFittedOnce = True
        finally:
            self.updateGUI()

    def onResetViews(self):
        if self.logic.imageNodes:
            channels = [ch for ch in self.logic.channels if ch in self.logic.imageNodes] or list(self.logic.imageNodes)
            self.logic.showInViews(channels, fitToViews=True, foregroundOpacity=self.ui.blendSliderWidget.value)

    def currentLesionMode(self):
        mode = self.ui.lesionModeComboBox.currentData
        return mode if mode in LESION_MODES else LESION_FILLED

    def setLesionModeInGUI(self, mode):
        self.ui.lesionModeComboBox.currentIndex = self.ui.lesionModeComboBox.findData(mode)

    def onLesionModeChanged(self, index):
        if not self._updatingGUI:
            self.logic.setLesionMode(self.currentLesionMode())

    def cycleLesionMode(self):
        current = LESION_MODES.index(self.currentLesionMode())
        self.setLesionModeInGUI(LESION_MODES[(current + 1) % len(LESION_MODES)])

    def onBlendChanged(self, value):
        if not self._updatingGUI:
            self.logic.setForegroundOpacity(value)

    def toggleChannel(self):
        """Jump between the two channels (foreground opacity 0 <-> 1)."""
        self.ui.blendSliderWidget.value = 0.0 if self.ui.blendSliderWidget.value > 0.5 else 1.0

    def updateBlendLabels(self):
        channels = self.logic.channels
        first = channels[0] if channels else _("Image 1")
        second = channels[1] if len(channels) > 1 else _("Image 2")
        self.ui.blendLabel.text = f"{first} \u25c0 \u25b6 {second}:"
        self.ui.blendSliderWidget.toolTip = _("0 shows only {first}, 1 shows only {second}. Same as the opacity slider in the view controllers. Shortcut: T").format(first=first, second=second)

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

    def onCommentTextChanged(self):
        if not self._updatingGUI and self.ui.commentTextEdit.plainText.strip():
            self.ui.rejectReasonLabel.hide()
            self.ui.commentTextEdit.setStyleSheet("")

    def onDecision(self, status):
        index = self.logic.currentIndex
        if self.logic.manifest is None or index < 0:
            return
        comment = self.ui.commentTextEdit.plainText
        if status == STATUS_REJECTED and not comment.strip():
            self.ui.rejectReasonLabel.show()
            self.ui.commentTextEdit.setStyleSheet("QPlainTextEdit { border: 2px solid #c83c3c; }")
            self.ui.commentTextEdit.setFocus()
            return
        try:
            self.logic.setDecision(index, status, reviewer=self.ui.reviewerLineEdit.text.strip(), comment=comment)
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
                lines = [f"{ch}: {path or '(missing)'}" for ch, path in case["images"].items()]
                lines.append(f"Segmentation: {case.get('segmentation') or '(none)'}")
                item.setToolTip("\n".join(lines))
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
            for widget in (self.ui.acceptButton, self.ui.rejectButton, self.ui.removeDecisionButton, self.ui.commentTextEdit):
                widget.enabled = hasCase
            self.ui.rejectReasonLabel.hide()
            self.ui.commentTextEdit.setStyleSheet("")
            if hasCase:
                self.ui.blendSliderWidget.value = self.logic.foregroundOpacity()

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
                for channel, path in case["images"].items():
                    lines.append(f"{channel}: {path or _('(missing)')}")
                lines.append(_("Segmentation: {path}").format(path=case.get("segmentation") or _("(none)")))
                self.ui.caseInfoLabel.text = "<br>".join(lines)
                self.ui.commentTextEdit.plainText = case.get("comment", "")
                self.ui.removeDecisionButton.enabled = status != STATUS_PENDING
            else:
                self.ui.caseInfoLabel.text = "-"
                self.ui.commentTextEdit.plainText = ""
        finally:
            self._updatingGUI = False


#
# DataReviewTest
#


class DataReviewTest(ScriptedLoadableModuleTest):
    """Tests the manifest logic on a small synthetic two-channel dataset and loads cases into the scene."""

    def setUp(self):
        slicer.mrmlScene.Clear()

    def runTest(self):
        self.setUp()
        self.test_DataReview1()

    def _writeSyntheticDataset(self, root, caseIds):
        import numpy as np
        for sub in ("adc", "tracew", "labels"):
            os.makedirs(os.path.join(root, sub))
        for i, caseId in enumerate(caseIds):
            shape = (12, 16, 20)
            rng = np.random.default_rng(i)
            adc = rng.normal(800, 100, shape).astype("float32")
            tracew = rng.normal(100, 20, shape).astype("float32")
            label = np.zeros(shape, dtype="uint8")
            label[4:8, 5:10, 6:12] = 1
            for array, path, className in (
                    (adc, os.path.join(root, "adc", f"{caseId}_adc.nii.gz"), None),
                    (tracew, os.path.join(root, "tracew", f"{caseId}_tracew.nii.gz"), None),
                    (label, os.path.join(root, "labels", f"{caseId}_seg.nii.gz"), "vtkMRMLLabelMapVolumeNode")):
                node = slicer.util.addVolumeFromArray(array, name="tmp", nodeClassName=className)
                self.assertTrue(slicer.util.saveNode(node, path))
                slicer.mrmlScene.RemoveNode(node)

    def test_DataReview1(self):
        self.delayDisplay("Creating synthetic dataset")
        root = slicer.util.tempDirectory("DataReviewTest")
        caseIds = ["sub-001", "sub-002", "sub-003"]
        self._writeSyntheticDataset(root, caseIds)

        self.delayDisplay("Scanning folder")
        patterns = {"ADC": "adc/*.nii.gz", "TRACEW": "tracew/*.nii.gz"}
        manifest = DataReviewLogic.scanDirectory(root, patterns, "labels/*.nii.gz")
        self.assertEqual(manifest["channels"], ["ADC", "TRACEW"])
        self.assertEqual([c["id"] for c in manifest["cases"]], caseIds)
        for case in manifest["cases"]:
            self.assertEqual(case["images"]["ADC"], os.path.join("adc", f"{case['id']}_adc.nii.gz"))
            self.assertEqual(case["images"]["TRACEW"], os.path.join("tracew", f"{case['id']}_tracew.nii.gz"))
            self.assertIsNotNone(case["segmentation"], f"segmentation not paired for {case['id']}")
            self.assertEqual(case["status"], STATUS_PENDING)

        self.delayDisplay("Case key derivation")
        self.assertEqual(_caseKey(os.path.join(root, "x", "sub-009_ADC.nii.gz"), root), "sub-009")
        self.assertEqual(_caseKey(os.path.join(root, "sub-010", "adc.nii.gz"), root), "sub-010")
        self.assertEqual(_caseKey(os.path.join(root, "sub-011", "seg.nrrd"), root), "sub-011")

        manifestPath = os.path.join(root, "review_manifest.json")
        logic = DataReviewLogic()
        logic.setManifest(manifest, manifestPath)
        logic.save()
        self.assertTrue(os.path.isfile(manifestPath))
        self.assertEqual(logic.manifest["root"], ".", "root should be stored relative to the manifest")
        self.assertEqual(logic.resolvePath(logic.case(0)["images"]["ADC"]), os.path.join(root, "adc", "sub-001_adc.nii.gz"))

        self.delayDisplay("Loading a case into the scene")
        imageNodes, segmentationNode = logic.loadCase(0, fitToViews=True)
        self.assertEqual(sorted(imageNodes.keys()), ["ADC", "TRACEW"])
        self.assertIsNotNone(segmentationNode)
        self.assertEqual(segmentationNode.GetSegmentation().GetNumberOfSegments(), 1)
        self.assertEqual(logic.currentIndex, 0)
        layoutManager = slicer.app.layoutManager()
        if layoutManager is not None:
            self.assertEqual(layoutManager.layout, REVIEW_LAYOUT_ID)
            for viewName, orientation in REVIEW_VIEWS.items():
                sliceWidget = layoutManager.sliceWidget(viewName)
                composite = sliceWidget.mrmlSliceCompositeNode()
                self.assertEqual(composite.GetBackgroundVolumeID(), imageNodes["ADC"].GetID())
                self.assertEqual(composite.GetForegroundVolumeID(), imageNodes["TRACEW"].GetID())
                self.assertTrue(composite.GetLinkedControl())
                self.assertEqual(sliceWidget.mrmlSliceNode().GetOrientation(), orientation)
            logic.setForegroundOpacity(1.0)
            self.assertEqual(logic.foregroundOpacity(), 1.0)
            self.assertEqual(layoutManager.sliceWidget("Green").mrmlSliceCompositeNode().GetForegroundOpacity(), 1.0)

        self.delayDisplay("Lesion display modes")
        displayNode = segmentationNode.GetDisplayNode()
        self.assertEqual(logic.lesionMode(), LESION_FILLED)
        logic.setLesionMode(LESION_OUTLINE)
        self.assertTrue(displayNode.GetVisibility() and displayNode.GetVisibility2DOutline() and not displayNode.GetVisibility2DFill())
        self.assertEqual(logic.lesionMode(), LESION_OUTLINE)
        logic.setLesionMode(LESION_HIDDEN)
        self.assertFalse(displayNode.GetVisibility())
        self.assertEqual(logic.lesionMode(), LESION_HIDDEN)
        _, seg2 = logic.loadCase(1, lesionMode=LESION_OUTLINE)
        self.assertEqual(logic.lesionMode(), LESION_OUTLINE)
        with self.assertRaises(ValueError):
            logic.setLesionMode("sparkly")

        self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")), 2, "previous case should be removed")
        self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLSegmentationNode")), 1)

        self.delayDisplay("Recording decisions")
        logic.setDecision(0, STATUS_ACCEPTED, reviewer="tester")
        with self.assertRaises(ValueError):
            logic.setDecision(1, STATUS_REJECTED, reviewer="tester", comment="   ")
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
        rescanned = DataReviewLogic.mergeReviewState(DataReviewLogic.scanDirectory(root, patterns, "labels/*.nii.gz"), logic2.manifest)
        self.assertEqual(rescanned["cases"][1]["status"], STATUS_REJECTED)

        self.delayDisplay("Version 1 manifests are upgraded")
        v1 = {"cases": [{"id": "a", "image": "adc/sub-001_adc.nii.gz"}]}
        DataReviewLogic.validate(v1)
        self.assertEqual(v1["cases"][0]["images"], {"Image": "adc/sub-001_adc.nii.gz"})
        self.assertEqual(v1["channels"], ["Image"])

        logic.setDecision(0, STATUS_PENDING)
        self.assertEqual(logic.case(0)["reviewer"], "")

        logic.unloadCurrentCase()
        self.delayDisplay("Test passed")
