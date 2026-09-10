cmake_minimum_required(VERSION 3.16)
project(chaozhenyun_decision_lib LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_executable(chaozhenyun_scheduler src/main.cpp src/ga.cpp)
