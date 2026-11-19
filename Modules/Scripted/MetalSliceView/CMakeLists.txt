set(MODULE_NAME MetalSliceView)

enable_language(OBJCXX)
find_library(APPKIT_FRAMEWORK AppKit REQUIRED)
find_library(METAL_FRAMEWORK Metal REQUIRED)
find_library(QUARTZCORE_FRAMEWORK QuartzCore REQUIRED)

add_library(MetalSliceViewNative SHARED MetalSliceViewNative.mm)
set_target_properties(MetalSliceViewNative PROPERTIES
  PREFIX ""
  OBJCXX_STANDARD 17
  OBJCXX_STANDARD_REQUIRED ON
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/${Slicer_QTSCRIPTEDMODULES_LIB_DIR}/$<0:>"
  )
target_compile_options(MetalSliceViewNative PRIVATE -fobjc-arc)
target_link_libraries(MetalSliceViewNative PRIVATE
  ${APPKIT_FRAMEWORK}
  ${METAL_FRAMEWORK}
  ${QUARTZCORE_FRAMEWORK}
  )
install(TARGETS MetalSliceViewNative
  LIBRARY DESTINATION ${Slicer_INSTALL_QTSCRIPTEDMODULES_LIB_DIR} COMPONENT Runtime
  )

slicerMacroBuildScriptedModule(
  NAME ${MODULE_NAME}
  SCRIPTS ${MODULE_NAME}.py
  )
add_dependencies(${MODULE_NAME} MetalSliceViewNative)
