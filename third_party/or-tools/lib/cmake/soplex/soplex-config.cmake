if(NOT TARGET libsoplex)
  include("${CMAKE_CURRENT_LIST_DIR}/soplex-targets.cmake")
endif()

include(CMakeFindDependencyMacro)

list(APPEND CMAKE_MODULE_PATH ${CMAKE_CURRENT_LIST_DIR})

set(SOPLEX_LIBRARIES libsoplex )
set(SOPLEX_PIC_LIBRARIES libsoplex-pic )
set(SOPLEX_INCLUDE_DIRS "${CMAKE_CURRENT_LIST_DIR}/../../../include")
set(SOPLEX_FOUND TRUE)

# If SoPlex was built with MPFR then we also need it.
set(SOPLEX_WITH_MPFR )
if(SOPLEX_WITH_MPFR)
   if(NOT MPFR_DIR)
      set(MPFR_DIR "")
   endif()
   find_dependency(MPFR)

   # TODO: Once we use targets, this will not be needed.
   set(SOPLEX_INCLUDE_DIRS ${SOPLEX_INCLUDE_DIRS} ${MPFR_INCLUDE_DIRS})
endif()

if(on)
  find_package(Boost 1.65.0)
  set(libs ${libs} Boost::boost)
endif()

if(off)
  set(SOPLEX_WITH_PAPILO TRUE)
endif()
