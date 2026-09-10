#----------------------------------------------------------------
# Generated CMake target import file for configuration "Release".
#----------------------------------------------------------------

# Commands may need to know the format version.
set(CMAKE_IMPORT_FILE_VERSION 1)

# Import target "BZip2::BZip2" for configuration "Release"
set_property(TARGET BZip2::BZip2 APPEND PROPERTY IMPORTED_CONFIGURATIONS RELEASE)
set_target_properties(BZip2::BZip2 PROPERTIES
  IMPORTED_LOCATION_RELEASE "${_IMPORT_PREFIX}/lib/libbz2.so.1.0.9"
  IMPORTED_SONAME_RELEASE "libbz2.so.1"
  )

list(APPEND _cmake_import_check_targets BZip2::BZip2 )
list(APPEND _cmake_import_check_files_for_BZip2::BZip2 "${_IMPORT_PREFIX}/lib/libbz2.so.1.0.9" )

# Commands beyond this point should not need to know the version.
set(CMAKE_IMPORT_FILE_VERSION)
