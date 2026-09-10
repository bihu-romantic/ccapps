#----------------------------------------------------------------
# Generated CMake target import file for configuration "Release".
#----------------------------------------------------------------

# Commands may need to know the format version.
set(CMAKE_IMPORT_FILE_VERSION 1)

# Import target "Coin::Clp" for configuration "Release"
set_property(TARGET Coin::Clp APPEND PROPERTY IMPORTED_CONFIGURATIONS RELEASE)
set_target_properties(Coin::Clp PROPERTIES
  IMPORTED_LOCATION_RELEASE "${_IMPORT_PREFIX}/lib/libClp.so.1.17.10"
  IMPORTED_SONAME_RELEASE "libClp.so.1"
  )

list(APPEND _cmake_import_check_targets Coin::Clp )
list(APPEND _cmake_import_check_files_for_Coin::Clp "${_IMPORT_PREFIX}/lib/libClp.so.1.17.10" )

# Import target "Coin::OsiClp" for configuration "Release"
set_property(TARGET Coin::OsiClp APPEND PROPERTY IMPORTED_CONFIGURATIONS RELEASE)
set_target_properties(Coin::OsiClp PROPERTIES
  IMPORTED_LOCATION_RELEASE "${_IMPORT_PREFIX}/lib/libOsiClp.so.1.17.10"
  IMPORTED_SONAME_RELEASE "libOsiClp.so.1"
  )

list(APPEND _cmake_import_check_targets Coin::OsiClp )
list(APPEND _cmake_import_check_files_for_Coin::OsiClp "${_IMPORT_PREFIX}/lib/libOsiClp.so.1.17.10" )

# Import target "Coin::ClpSolver" for configuration "Release"
set_property(TARGET Coin::ClpSolver APPEND PROPERTY IMPORTED_CONFIGURATIONS RELEASE)
set_target_properties(Coin::ClpSolver PROPERTIES
  IMPORTED_LOCATION_RELEASE "${_IMPORT_PREFIX}/lib/libClpSolver.so.1.17.10"
  IMPORTED_SONAME_RELEASE "libClpSolver.so.1"
  )

list(APPEND _cmake_import_check_targets Coin::ClpSolver )
list(APPEND _cmake_import_check_files_for_Coin::ClpSolver "${_IMPORT_PREFIX}/lib/libClpSolver.so.1.17.10" )

# Commands beyond this point should not need to know the version.
set(CMAKE_IMPORT_FILE_VERSION)
