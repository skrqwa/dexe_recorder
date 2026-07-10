cmake_minimum_required(VERSION 3.15)
project(dexe_recorder)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_BUILD_TYPE Release)

find_program(CCACHE_PROGRAM ccache)
if(CCACHE_PROGRAM)
  set(CMAKE_C_COMPILER_LAUNCHER "${CCACHE_PROGRAM}")
  set(CMAKE_CXX_COMPILER_LAUNCHER "${CCACHE_PROGRAM}")
endif()

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(rclcpp_components REQUIRED)
find_package(ament_index_cpp REQUIRED)
find_package(std_msgs REQUIRED)
find_package(std_srvs REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(end_effector_interfaces REQUIRED)
find_package(yaml-cpp REQUIRED)
find_package(nlohmann_json REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(GST REQUIRED gstreamer-1.0 gstreamer-app-1.0 gstreamer-video-1.0)

add_executable(dexe_recorder_node
  src/dexe_recorder_node.cpp
  src/recorder_node.cpp
  src/frame_buffer.cpp
  src/gst_recorder.cpp
)

target_include_directories(dexe_recorder_node PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src
  ${CMAKE_CURRENT_SOURCE_DIR}/include
)

ament_target_dependencies(dexe_recorder_node
  rclcpp
  rclcpp_components
  ament_index_cpp
  std_msgs
  std_srvs
  sensor_msgs
  end_effector_interfaces
)

target_link_libraries(dexe_recorder_node
  yaml-cpp
  nlohmann_json::nlohmann_json
  ${GST_LIBRARIES}
)

target_include_directories(dexe_recorder_node PRIVATE
  ${GST_INCLUDE_DIRS}
)

install(TARGETS dexe_recorder_node
  DESTINATION lib/${PROJECT_NAME}
)

install(DIRECTORY config
  DESTINATION share/${PROJECT_NAME}
)

install(PROGRAMS scripts/run_recorder.sh scripts/recorderctl.sh scripts/convert_to_hdf5.sh scripts/convert_to_hdf5.py deploy.sh
  DESTINATION share/${PROJECT_NAME}
)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  ament_lint_auto_find_test_dependencies()
endif()

ament_package()
