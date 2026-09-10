#----------------------------------------------------------------
# Generated CMake target import file for configuration "Release".
#----------------------------------------------------------------

# Commands may need to know the format version.
set(CMAKE_IMPORT_FILE_VERSION 1)

# Import target "SCIP::scip" for configuration "Release"
set_property(TARGET SCIP::scip APPEND PROPERTY IMPORTED_CONFIGURATIONS RELEASE)
set_target_properties(SCIP::scip PROPERTIES
  IMPORTED_LOCATION_RELEASE "${_IMPORT_PREFIX}/bin/scip"
  )

list(APPEND _cmake_import_check_targets SCIP::scip )
list(APPEND _cmake_import_check_files_for_SCIP::scip "${_IMPORT_PREFIX}/bin/scip" )

# Import target "SCIP::libscip" for configuration "Release"
set_property(TARGET SCIP::libscip APPEND PROPERTY IMPORTED_CONFIGURATIONS RELEASE)
set_target_properties(SCIP::libscip PROPERTIES
  IMPORTED_LOCATION_RELEASE "${_IMPORT_PREFIX}/lib/libscip.so.10.0.0"
  IMPORTED_SONAME_RELEASE "libscip.so.10.0"
  )

list(APPEND _cmake_import_check_targets SCIP::libscip )
list(APPEND _cmake_import_check_files_for_SCIP::libscip "${_IMPORT_PREFIX}/lib/libscip.so.10.0.0" )

# Commands beyond this point should not need to know the version.
set(CMAKE_IMPORT_FILE_VERSION)
