#----------------------------------------------------------------
# Generated CMake target import file for configuration "Release".
#----------------------------------------------------------------

# Commands may need to know the format version.
set(CMAKE_IMPORT_FILE_VERSION 1)

# Import target "Coin::Cbc" for configuration "Release"
set_property(TARGET Coin::Cbc APPEND PROPERTY IMPORTED_CONFIGURATIONS RELEASE)
set_target_properties(Coin::Cbc PROPERTIES
  IMPORTED_LOCATION_RELEASE "${_IMPORT_PREFIX}/lib/libCbc.so.2.10.12"
  IMPORTED_SONAME_RELEASE "libCbc.so.2"
  )

list(APPEND _cmake_import_check_targets Coin::Cbc )
list(APPEND _cmake_import_check_files_for_Coin::Cbc "${_IMPORT_PREFIX}/lib/libCbc.so.2.10.12" )

# Import target "Coin::OsiCbc" for configuration "Release"
set_property(TARGET Coin::OsiCbc APPEND PROPERTY IMPORTED_CONFIGURATIONS RELEASE)
set_target_properties(Coin::OsiCbc PROPERTIES
  IMPORTED_LOCATION_RELEASE "${_IMPORT_PREFIX}/lib/libOsiCbc.so.2.10.12"
  IMPORTED_SONAME_RELEASE "libOsiCbc.so.2"
  )

list(APPEND _cmake_import_check_targets Coin::OsiCbc )
list(APPEND _cmake_import_check_files_for_Coin::OsiCbc "${_IMPORT_PREFIX}/lib/libOsiCbc.so.2.10.12" )

# Import target "Coin::CbcSolver" for configuration "Release"
set_property(TARGET Coin::CbcSolver APPEND PROPERTY IMPORTED_CONFIGURATIONS RELEASE)
set_target_properties(Coin::CbcSolver PROPERTIES
  IMPORTED_LOCATION_RELEASE "${_IMPORT_PREFIX}/lib/libCbcSolver.so.2.10.12"
  IMPORTED_SONAME_RELEASE "libCbcSolver.so.2"
  )

list(APPEND _cmake_import_check_targets Coin::CbcSolver )
list(APPEND _cmake_import_check_files_for_Coin::CbcSolver "${_IMPORT_PREFIX}/lib/libCbcSolver.so.2.10.12" )

# Commands beyond this point should not need to know the version.
set(CMAKE_IMPORT_FILE_VERSION)
