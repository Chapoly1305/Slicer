"""Optional 10-bit grayscale slice presentation on macOS."""

import ctypes
import logging
import sys
from pathlib import Path

import numpy as np
import qt
import slicer
import vtk
from slicer.ScriptedLoadableModule import ScriptedLoadableModule, ScriptedLoadableModuleWidget
from vtk.util.numpy_support import vtk_to_numpy


class PixelReport(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint32)
        for name in ("format", "width", "height", "grayLevels", "nonGrayPixels", "minCode", "maxCode", "maxSourceCodeError")
    ]

    def values(self):
        return {field: getattr(self, field) for field, _ in self._fields_}


def loadNativeLibrary(path):
    library = ctypes.CDLL(str(path))
    library.SlicerMetalSliceAttach.argtypes = [ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t]
    library.SlicerMetalSliceAttach.restype = ctypes.c_void_p
    library.SlicerMetalSliceDetach.argtypes = [ctypes.c_void_p]
    library.SlicerMetalSliceDetach.restype = None
    library.SlicerMetalSliceDraw.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
        ctypes.c_float, ctypes.c_float, ctypes.POINTER(PixelReport), ctypes.c_char_p, ctypes.c_size_t,
    ]
    library.SlicerMetalSliceDraw.restype = ctypes.c_int
    library.SlicerMetalSliceRamp.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(PixelReport), ctypes.c_char_p, ctypes.c_size_t,
    ]
    library.SlicerMetalSliceRamp.restype = ctypes.c_int
    return library


class MetalSliceController:
    """Mirror the high-precision background of one Slicer slice widget."""

    def __init__(self, sliceName, libraryPath):
        self.sliceName = sliceName
        self.layoutManager = slicer.app.layoutManager()
        self.sliceWidget = self.layoutManager.sliceWidget(sliceName)
        if self.sliceWidget is None:
            raise RuntimeError(f"Slice {sliceName} does not exist")
        self.view = self.sliceWidget.sliceView()
        if not self.view.isVisible():
            raise RuntimeError(f"Slice {sliceName} is not visible")
        if not libraryPath.is_file():
            raise RuntimeError(f"Metal backend is missing: {libraryPath}")
        self.library = loadNativeLibrary(libraryPath)
        self.signature = None
        self.handle = None
        self.timer = qt.QTimer(slicer.util.mainWindow())
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.onTimer)
        self.sceneObserver = slicer.mrmlScene.AddObserver(slicer.vtkMRMLScene.StartCloseEvent, self.onSceneClose)

    def start(self):
        # Validate the source before creating a child NSView on the Qt slice widget.
        self.readSource()
        error = ctypes.create_string_buffer(2048)
        self.handle = self.library.SlicerMetalSliceAttach(int(self.view.winId()), error, len(error))
        if not self.handle:
            raise RuntimeError(error.value.decode("utf-8", errors="replace"))
        self.timer.start()

    def readSource(self):
        logic = self.sliceWidget.sliceLogic()
        composite = logic.GetSliceCompositeNode()
        if (composite.GetForegroundVolumeID() and composite.GetForegroundOpacity() > 0) or (
            composite.GetLabelVolumeID() and composite.GetLabelOpacity() > 0
        ):
            raise RuntimeError("Metal 10-bit mode requires a grayscale background without volume overlays")
        layer = logic.GetBackgroundLayer()
        volume = layer.GetVolumeNode()
        if volume is None:
            raise RuntimeError("The selected slice has no background volume")
        display = volume.GetDisplayNode()
        if display is None or not display.IsA("vtkMRMLScalarVolumeDisplayNode") or display.GetWindow() <= 0:
            raise RuntimeError("The selected volume has no valid window and level")
        if (
            display.GetColorNodeID() != "vtkMRMLColorTableNodeGrey"
            or display.GetWindowMappingMethod() != 0
            or display.GetInvertDisplayScalarRange()
            or display.GetApplyThreshold()
        ):
            raise RuntimeError("Metal slice view requires a linear, non-inverted grayscale window without thresholds")
        reslice = layer.GetReslice()
        reslice.Update()
        image = reslice.GetOutput()
        width, height, depth = image.GetDimensions()
        if (
            image.GetScalarType() != vtk.VTK_UNSIGNED_SHORT
            or image.GetNumberOfScalarComponents() != 1
            or depth != 1
            or min(width, height) <= 0
        ):
            raise RuntimeError("Metal slice view requires one plane of unsigned 16-bit grayscale reslice data")
        return image, width, height, display.GetWindow(), display.GetLevel()

    def draw(self, verify=False):
        image, width, height, window, level = self.readSource()
        scalars = image.GetPointData().GetScalars()
        signature = (
            image.GetMTime(), scalars.GetMTime(), width, height, window, level, self.view.devicePixelRatioF()
        )
        if not verify and signature == self.signature:
            return None
        pixels = np.ascontiguousarray(vtk_to_numpy(scalars))
        error = ctypes.create_string_buffer(2048)
        report = PixelReport() if verify else None
        ok = self.library.SlicerMetalSliceDraw(
            self.handle, pixels.ctypes.data, width, height, level - window / 2, window,
            ctypes.byref(report) if report is not None else None, error, len(error),
        )
        if not ok:
            raise RuntimeError(error.value.decode("utf-8", errors="replace"))
        self.signature = signature
        return report.values() if report is not None else None

    def verify(self):
        error = ctypes.create_string_buffer(2048)
        ramp = PixelReport()
        if not self.library.SlicerMetalSliceRamp(self.handle, ctypes.byref(ramp), error, len(error)):
            raise RuntimeError(error.value.decode("utf-8", errors="replace"))
        if ramp.grayLevels != 1024 or ramp.nonGrayPixels != 0 or ramp.maxSourceCodeError > 1:
            raise RuntimeError(f"The Metal pipeline did not preserve all 1024 neutral gray codes: {ramp.values()}")
        drawable = self.draw(verify=True)
        if drawable["format"] != ramp.format or drawable["nonGrayPixels"] != 0 or drawable["maxSourceCodeError"] > 1:
            raise RuntimeError(f"The displayed slice differs from the tested grayscale pipeline: {drawable}")
        return {"slice": self.sliceName, "ramp": ramp.values(), "drawable": drawable}

    def onTimer(self):
        if not self.handle:
            return
        try:
            if self.view.isVisible():
                self.draw()
            else:
                self.signature = None
        except (RuntimeError, ValueError):
            logging.exception("Metal slice view stopped")
            self.close()

    def onSceneClose(self, _caller, _event):
        self.close()

    def close(self):
        if self.timer is not None:
            self.timer.stop()
            self.timer.deleteLater()
            self.timer = None
        if self.sceneObserver is not None:
            slicer.mrmlScene.RemoveObserver(self.sceneObserver)
            self.sceneObserver = None
        if self.handle:
            self.library.SlicerMetalSliceDetach(self.handle)
            self.handle = None
            self.view.scheduleRender()


def disable():
    controller = getattr(slicer, "_metalSliceController", None)
    if controller is not None:
        controller.close()
        slicer._metalSliceController = None


def enable(sliceName="Red", libraryPath=None):
    if sys.platform != "darwin":
        raise RuntimeError("Metal slice view is available on macOS")
    disable()
    path = Path(libraryPath) if libraryPath else Path(__file__).with_name("MetalSliceViewNative.dylib")
    controller = MetalSliceController(sliceName, path)
    try:
        controller.start()
        slicer.app.processEvents()
        result = controller.verify()
    except Exception:
        controller.close()
        raise
    slicer._metalSliceController = controller
    return result


class MetalSliceView(ScriptedLoadableModule):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent.title = "Metal 10-bit Slice View"
        self.parent.categories = ["Utilities"]
        self.parent.helpText = "Display an unsigned 16-bit grayscale slice with a 10-bit-per-channel Metal drawable."


class MetalSliceViewWidget(ScriptedLoadableModuleWidget):
    def setup(self):
        super().setup()
        self.sliceName = qt.QComboBox()
        self.sliceName.addItems(["Red", "Yellow", "Green"])
        self.layout.addWidget(self.sliceName)
        activate = qt.QPushButton("Enable 10-bit grayscale")
        activate.clicked.connect(self.activate)
        self.layout.addWidget(activate)
        deactivate = qt.QPushButton("Restore standard slice rendering")
        deactivate.clicked.connect(self.deactivate)
        self.layout.addWidget(deactivate)
        self.status = qt.QLabel("")
        self.status.setWordWrap(True)
        controller = getattr(slicer, "_metalSliceController", None)
        if controller is not None and controller.handle:
            self.status.text = f"Metal 10-bit is active on the {controller.sliceName} slice."
        self.layout.addWidget(self.status)
        self.layout.addStretch(1)

    def activate(self):
        try:
            result = enable(self.sliceName.currentText)
            self.status.text = f"Metal drawable verified. {result['drawable']['grayLevels']} gray codes in the current slice."
        except Exception as exc:
            self.status.text = str(exc)

    def deactivate(self):
        disable()
        self.status.text = "Standard slice rendering is active."

    def cleanup(self):
        disable()
        super().cleanup()
