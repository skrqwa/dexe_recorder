# GStreamer + OpenCV 冲突踩坑记录

> 日期: 2026-07-07 | 模块: dexe_recorder (C++ GStreamer 录制)

## 现象

`dexe_recorder_node` 在 `start_recording` 服务调用时崩溃：

```
[INFO] dexe_recorder ready. state_topic=/feedback/robot_server_state
[ros2run]: Bus error
```

- 崩溃发生在 `gst_parse_launch()` 内部，调用栈完全损坏
- GDB 显示 `pc` / `sp` 指向无效地址（`0x186888d510e21f`），属于内存损坏
- **同样的 pipeline 用 `gst-launch-1.0` 命令行运行完全正常**
- **同样的 pipeline 用 Python + PyGObject（遥操 `gst_jpeg_writer.py`）运行完全正常**

## 初步排查

按最小化验证法逐步隔离冲突源：

| 测试 | 组合 | 结果 |
|------|------|------|
| 独立 C 程序 | gstreamer only（无 rclcpp/opencv） | ✅ 正常 |
| 包内测试可执行 | rclcpp + gstreamer（无 opencv） | ✅ 正常 |
| 完整节点 | rclcpp + gstreamer + **OpenCV** | ❌ Bus error |
| Python 脚本 | rclpy + PyGObject | ✅ 正常 |

初步定位 OpenCV 相关，但需进一步验证是"链接"、"include"还是"cv:: 调用"导致。

## 根因

> 以下结论基于三组对照实验验证（见下文"验证过程"），非推测。

**根因：源码中包含 `cv::` API 调用**（即使是不执行的死代码）会导致 OpenCV 共享库在运行时被加载执行初始化代码，与 `nvjpegdec` 的 NVMM 硬件资源初始化冲突，触发 SIGBUS。

- `nvjpegdec` 是 NVIDIA 硬件 JPEG 解码器，使用 NVMM（NVIDIA Multimedia Memory）内存
- OpenCV（Jetson 上的 `opencv4tegra`）共享库加载时会执行 `.init_array` 初始化代码，访问 NVIDIA 硬件资源
- 两者在同一 C++ 进程中同时初始化时，会导致 NVMM 内存损坏
- 遥操用 Python（PyGObject）调用 GStreamer，不受影响

**关键细节**：仅 `#include <opencv2/...>` 头文件但不调用任何 `cv::` 函数时，链接器（`--as-needed` 模式）不会实际拉入 OpenCV 共享库，所以不崩溃。只有源码中引用了 `cv::` 符号（如 `cv::Mat`、`cv::cvtColor`、`cv::imencode`），链接器才会解析并拉入 OpenCV 共享库，运行时加载执行其初始化代码导致冲突。

## 验证过程

### 第一轮：定位冲突源（三组对照实验）

在移除 OpenCV 后正常工作的代码上逐步加回 OpenCV：

| 验证 | OpenCV 链接 | OpenCV `#include` | `cv::` API 调用 | 结果 |
|------|-----------|-------------------|----------------|------|
| A | ✅ `${OpenCV_LIBS}` | ❌ | ❌ | ✅ 正常 |
| B | ✅ | ✅ | ✅（死代码 `WriteImage()`，无调用者） | ❌ Bus error |
| C | ✅ | ✅ | ❌ | ✅ 正常 |

- **验证 A**：CMakeLists 链接 OpenCV，源码不含任何 OpenCV 引用 → 正常（链接器 `--as-needed` 跳过未使用的库）
- **验证 B**：加回 `#include` + `WriteImage()` 函数（含 `cv::Mat`/`cv::cvtColor`/`cv::imencode`，但无调用者）→ Bus error 复现
- **验证 C**：只保留 `#include`，删除 `WriteImage()` 函数体 → 正常（有头文件引用但无符号调用，链接器不拉入库）

### 第二轮：深入验证加载方式与顺序（dlopen 实验）

用 C++ dlopen 模拟 Python `import` 机制，排除"加载方式"假设：

| # | 方式 | 加载顺序 | 结果 |
|---|------|---------|------|
| 1 | C++ 直接链接 OpenCV（DT_NEEDED） | OpenCV → gst_init | ❌ Bus error |
| 2 | C++ dlopen OpenCV | OpenCV → gst_init | ❌ Segfault |
| 3 | C++ dlopen OpenCV | gst_init → OpenCV | ✅ 正常 |
| 4 | Python `import cv2` | cv2 → Gst.init | ✅ 正常 |

**关键矛盾**：#2 和 #4 加载顺序一样（OpenCV 先，GStreamer 后），但 C++ 崩溃 Python 不崩溃。

### 为什么 Python 不崩溃

Python `import cv2` 和 C++ `dlopen("libopencv_core.so")` 加载顺序相同，但 Python 不崩溃。可能原因：

1. Python 进程启动时已加载大量基础设施库（libpython, libpthread 等），预先初始化了某些硬件状态
2. Python cv2 通过 `cv2.cpython-310-aarch64-linux-gnu.so`（7.3MB wrapper）间接加载 OpenCV，依赖链和初始化路径与直接 dlopen 不同
3. Python 的 GIL 可能影响了 `.init_array` 的执行时序

具体机制需更深入分析（对比 `/proc/self/maps` 和 `.init_array` 执行顺序），但实际使用结论已明确。

### C++ 中如何避免冲突

**方案：先 `gst_init()`，再加载 OpenCV**（验证 #3 已证明可行）

```cpp
// 1. 先初始化 GStreamer
gst_init(&argc, &argv);
// 2. 再 dlopen OpenCV（延迟加载，模拟 Python import）
void* handle = dlopen("libopencv_core.so.4.5d", RTLD_NOW | RTLD_GLOBAL);
// 之后可以安全使用 cv:: API
```

不能直接链接 OpenCV（`DT_NEEDED` 会在 `main()` 之前加载 OpenCV，早于 `gst_init`）。

**结论**：对于 dexe_recorder，最简单也最正确的方案是完全移除 OpenCV——所有图像处理都在 GStreamer pipeline 内完成，不需要 OpenCV。

## 解决方案

### 1. 移除 OpenCV 依赖

`dexe_recorder` 本不需要 OpenCV——所有图像编解码都在 GStreamer pipeline 内完成。

**CMakeLists.txt** 删除：
```cmake
find_package(OpenCV REQUIRED)          # 删除
target_link_libraries(... ${OpenCV_LIBS})  # 删除 ${OpenCV_LIBS}
```

**recorder_node.cpp** 删除：
```cpp
#include <opencv2/imgcodecs.hpp>   // 删除
#include <opencv2/imgproc.hpp>     // 删除
// 以及 WriteImage() 函数体（死代码，无调用者）
```

### 2. Pipeline 设计（NVMM 内存转换）

`nvjpegdec` 输出 NVMM 内存，软件 `videoconvert`/`videocrop` 无法直接访问。需要用 `nvvidconv` 做内存转换：

```
appsrc(JPEG) → jpegparse → nvjpegdec → nvvidconv(NVMM→sysmem)
  → video/x-raw,format=I420 → tee
  → 左路: videocrop(left=0,right=960) → nvvidconv(sysmem→NVMM) → nvv4l2h264enc → qtmux → filesink
  → 右路: videocrop(left=960,right=0) → nvvidconv(sysmem→NVMM) → nvv4l2h264enc → qtmux → filesink
```

关键点：
- `nvjpegdec` → `nvvidconv`（无 caps 限制）→ `video/x-raw,format=I420`：NVMM 转系统内存
- `videocrop` 后 → `nvvidconv` → `video/x-raw(memory:NVMM),format=I420`：系统内存转回 NVMM 给编码器
- 不能用 `videoconvert`（软件）处理 `nvjpegdec` 的 NVMM 输出

## 验证结果

修复后 VIDEO 模式录制：
- ✅ 节点不再崩溃
- ✅ head/left.mp4 + head/right.mp4 各 63MB（12s 录制）
- ✅ H.264 Constrained Baseline，960×1080（正确左右目拆分）
- ✅ 1128 frames, 0 dropped
- ✅ gst-discoverer 确认视频流有效

## 经验总结

1. **C++ ROS2 节点在 Jetson 上慎用 OpenCV + GStreamer 组合**：尤其是涉及 `nvjpegdec`/`nvv4l2h264enc` 等硬件加速插件
2. **Python（PyGObject）不受影响**：遥操用 Python 调 GStreamer 是安全的
3. **排查方法**：写独立 C/C++ 测试程序（不含 rclcpp/opencv）逐步隔离依赖
4. **GDB 对内存损坏的 backtrace 不可靠**：pc/sp 指向无效地址时，需用日志/二分法定位
5. **NVMM 内存的特殊性**：`nvjpegdec` 输出 NVMM，软件 element 无法直接处理，必须用 `nvvidconv` 转换
