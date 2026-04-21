cmake_minimum_required(VERSION 3.16)
project(navmesh_wrapper)

find_package(Python3 REQUIRED COMPONENTS Interpreter Development)
find_package(pybind11 CONFIG REQUIRED)

pybind11_add_module(navmesh navmesh.cpp)

target_include_directories(navmesh PRIVATE /usr/include/recastnavigation)
target_link_libraries(navmesh PRIVATE Recast Detour)
