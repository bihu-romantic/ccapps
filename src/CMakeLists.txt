# Uncomment the next line to see the compiler commands
# set(CMAKE_VERBOSE_MAKEFILE ON)
configure_file(
  "${CMAKE_CURRENT_SOURCE_DIR}/ccAPPS.ico"
  "${CMAKE_CURRENT_BINARY_DIR}/ccAPPS.ico"
  COPYONLY
)

# Auxilary static libraries
add_library(utils STATIC
  "utils/actions.cpp"
  "utils/date.cpp"
  "utils/library.cpp"
  "utils/python.cpp"
  "utils/xml.cpp"
  "utils/cache.cpp"
  "utils/database.cpp"
  "utils/json.cpp"
  "${CMAKE_SOURCE_DIR}/include/ccAPPS/utils.h"
  "${CMAKE_SOURCE_DIR}/include/ccAPPS/cache.h"
  "${CMAKE_SOURCE_DIR}/include/ccAPPS/database.h"
  "${CMAKE_SOURCE_DIR}/include/ccAPPS/xml.h"
  "${CMAKE_SOURCE_DIR}/include/ccAPPS/json.h"
  "${CMAKE_BINARY_DIR}/include/config.h"
)
add_dependencies(utils venv)

add_library(model STATIC
  "model/actions.cpp"
  "model/buffer.cpp"
  "model/calendar.cpp"
  "model/customer.cpp"
  "model/demand.cpp"
  "model/flow.cpp"
  "model/flowplan.cpp"
  "model/item.cpp"
  "model/itemdistribution.cpp"
  "model/itemsupplier.cpp"
  "model/leveled.cpp"
  "model/library.cpp"
  "model/load.cpp"
  "model/loadplan.cpp"
  "model/location.cpp"
  "model/operation.cpp"
  "model/operationdependency.cpp"
  "model/operationplan.cpp"
  "model/pegging.cpp"
  "model/plan.cpp"
  "model/problem.cpp"
  "model/problems_buffer.cpp"
  "model/problems_operationplan.cpp"
  "model/problems_resource.cpp"
  "model/resource.cpp"
  "model/resourceskill.cpp"
  "model/setupmatrix.cpp"
  "model/skill.cpp"
  "model/solver.cpp"
  "model/suboperation.cpp"
  "model/supplier.cpp"
  "${CMAKE_SOURCE_DIR}/include/ccAPPS/model.h"
)
add_dependencies(model utils)

add_library(solver STATIC
  "solver/operatordelete.cpp"
  "solver/solverbuffer.cpp"
  "solver/solverdemand.cpp"
  "solver/solverflow.cpp"
  "solver/solverload.cpp"
  "solver/solveroperation.cpp"
  "solver/solverplan.cpp"
  "solver/solverresource.cpp"
  "solver/solveraco.cpp"
  "solver/solverga.cpp"
  "solver/solvervns.cpp"
  "solver/solverexact.cpp"
  "solver/solverautofix.cpp"
  "solver/solverfas.cpp"
  "${CMAKE_SOURCE_DIR}/include/ccAPPS/solver.h"
  "${CMAKE_SOURCE_DIR}/include/ccAPPS/solveraco.h"
  "${CMAKE_SOURCE_DIR}/include/ccAPPS/solverga.h"
  "${CMAKE_SOURCE_DIR}/include/ccAPPS/solvervns.h"
  "${CMAKE_SOURCE_DIR}/include/ccAPPS/solverexact.h"
  "${CMAKE_SOURCE_DIR}/include/ccAPPS/solverautofix.h"
  "${CMAKE_SOURCE_DIR}/include/ccAPPS/solverfas.h"
)
add_dependencies(solver model)
if(CCAPPS_HAVE_ORTOOLS)
  target_compile_definitions(solver PRIVATE HAVE_ORTOOLS)
  if(TARGET ortools::ortools)
    target_link_libraries(solver PUBLIC ortools::ortools)
  else()
    target_include_directories(solver PRIVATE "${ORTOOLS_INCLUDE_DIR}")
    target_compile_definitions(solver PRIVATE OR_PROTO_DLL= USE_SCIP)
    target_link_libraries(solver PUBLIC "${ORTOOLS_LIBRARY}")
  endif()
endif()

add_library(forecast STATIC
  "forecast/exprtk.h"
  "forecast/forecast.cpp"
  "forecast/forecast.h"
  "forecast/forecastsolver.cpp"
  "forecast/measure.cpp"
  "forecast/measure_compute.cpp"
  "forecast/timeseries.cpp"
)

if(MSVC)
  set_target_properties(forecast PROPERTIES
    COMPILE_FLAGS "/bigobj"
  )
endif()

add_dependencies(forecast solver)

# Main shared library
add_library(ccAPPS SHARED
  dllmain.cpp
  tags.cpp
)
target_link_libraries(ccAPPS PUBLIC utils)
target_link_libraries(ccAPPS PUBLIC model)
target_link_libraries(ccAPPS PUBLIC solver)
target_link_libraries(ccAPPS PUBLIC xerces-c)
target_link_libraries(ccAPPS PUBLIC forecast)
if(CCAPPS_HAVE_ORTOOLS)
  set_target_properties(ccAPPS PROPERTIES
    BUILD_RPATH "${ORTOOLS_LIBRARY_DIR}"
    INSTALL_RPATH "${ORTOOLS_LIBRARY_DIR}"
  )
endif()

target_link_libraries(ccAPPS PRIVATE pq)
# Allow unresolved Python symbols — they resolve from the python binary
# when loaded via "import ccAPPS", or from the ccAPPS executable's own
# libpython dependency when loaded by the ccAPPS binary.
target_link_options(ccAPPS PRIVATE "LINKER:-allow-shlib-undefined")

install(
  TARGETS ccAPPS
  DESTINATION "${CMAKE_INSTALL_LIBDIR}"
)

# Main executable
add_executable(ccAPPS-main
  main.cpp
  "${CMAKE_SOURCE_DIR}/include/ccAPPSinterface.h"
)
target_link_libraries(ccAPPS-main LINK_PUBLIC ccAPPS)
target_link_libraries(ccAPPS-main PRIVATE ${Python3_LIBRARIES})
set_target_properties(ccAPPS-main PROPERTIES
  OUTPUT_NAME "ccAPPS"
  PDB_NAME_DEBUG "ccAPPS-main"
)
install(
  TARGETS ccAPPS-main
  DESTINATION "${CMAKE_INSTALL_BINDIR}"
)

# Put the compiled binaries in the source folder.
# This violates the out-of-source build principle, and mixes up multi-configuration builds.
# Only excuse is that it's convenient.
add_custom_command(
  TARGET ccAPPS POST_BUILD VERBATIM
  COMMAND ${CMAKE_COMMAND} -E copy "$<TARGET_FILE:ccAPPS>" "${CMAKE_SOURCE_DIR}/bin"
)
add_custom_command(
  TARGET ccAPPS-main POST_BUILD VERBATIM
  COMMAND ${CMAKE_COMMAND} -E copy "$<TARGET_FILE:ccAPPS-main>" "${CMAKE_SOURCE_DIR}/bin"
)
