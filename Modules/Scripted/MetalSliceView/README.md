# Metal 10-bit Slice View

This opt-in macOS module presents one unsigned 16-bit scalar slice on a Metal
`BGR10A2Unorm` drawable. It reads the Slicer background reslice before the 8-bit
window/level filter. The shader applies a linear window/level to an `R16Unorm`
texture. A native child view presents the result while the Slicer slice widget
continues to handle input.

The prototype supports a linear grayscale background. It presents the background
alone, so Slicer annotations and other overlays are available through standard slice
rendering. The GPU check compares the drawable's codes with the original reslice and
checks all 1,024 codes with a synthetic ramp. It does not measure the physical video link.

## Use with an existing Slicer

Run the following from the repository root on macOS with the Xcode command-line tools.

```sh
metal_module_dir=$(mktemp -d /tmp/slicer-metal10.XXXXXXXX)
cp Modules/Scripted/MetalSliceView/MetalSliceView.py "$metal_module_dir/"
xcrun clang++ -dynamiclib -std=c++17 -fobjc-arc \
  -framework AppKit -framework Metal -framework QuartzCore \
  Modules/Scripted/MetalSliceView/MetalSliceViewNative.mm \
  -o "$metal_module_dir/MetalSliceViewNative.dylib"
printf '%s\n' "$metal_module_dir"
```

In Slicer's Python console, register the staged module using the printed directory.

```python
import qt
import slicer

factory = slicer.app.moduleManager().factoryManager()
factory.registerModule(qt.QFileInfo("/tmp/slicer-metal10.XXXXXXXX/MetalSliceView.py"))
factory.loadModules(["MetalSliceView"])
slicer.util.selectModule("MetalSliceView")
```

Select the slice and click **Enable 10-bit grayscale**. Use **Restore standard
slice rendering** to remove the Metal view.

The fork's macOS CMake build can include the module and its library in a new
application package. Loading the staged module in an existing process uses the
same renderer without requiring that package.
