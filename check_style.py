#!/usr/bin/env python3
"""通用 C++ 代码风格检查工具

自动扫描当前目录下的 src/ 和 include/ 目录中的 .h/.cpp/.hpp 文件，
检查 clang-format 格式和 DexForce 版权头。

依赖：
  - clang-format >= 14.0.0
  - chardet（pip install chardet）

用法：
  python3 check_style.py              # 检查格式
  python3 check_style.py --apply      # 检查并自动修复
  python3 check_style.py --verbose    # 打印所有文件名
"""

import argparse
import datetime
import os
import re
import subprocess
import time

import chardet

# 要扫描的源码目录（不存在的目录自动跳过）
CPP_FORMAT_DIRS = ["src", "include", "camera"]
# 要排除的第三方代码目录（不存在的自动跳过）
CPP_FORMAT_EXCLUDE_DIRS = [
    os.path.join("src", "3rd-party"),
    os.path.join("src", "protobuf"),
    os.path.join("camera", "third_party"),
]

HEADER_YEAR_BEGIN = 2021
HEADER_YEAR_END = datetime.datetime.now().year


def check_header(file_path, standard_header, header_pattern):
    """检查文件头部是否缺少标准头部，或者存在头部但年份错误"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            if not content.startswith(standard_header):
                match = re.match(header_pattern, content)
                if match:
                    start_year, end_year = match.groups()
                    if int(start_year) != HEADER_YEAR_BEGIN or int(end_year) != HEADER_YEAR_END:
                        return "incorrect_year"
                else:
                    return "missing"
            else:
                return "correct"
    except Exception as e:
        print(f"Error reading file {file_path}: {e}")
        return "error"


def add_header(file_path, header):
    """向文件添加头部"""
    try:
        with open(file_path, "r+", encoding="utf-8") as f:
            content = f.read()
            f.seek(0, 0)
            f.write(header + "\n" + content)
    except Exception as e:
        print(f"Error adding header to file {file_path}: {e}")


def replace_header(file_path, header, header_pattern):
    """替换文件头部"""
    try:
        with open(file_path, "r+", encoding="utf-8") as f:
            content = f.read()
            new_content = re.sub(header_pattern, header, content)
            f.seek(0, 0)
            f.write(new_content)
    except Exception as e:
        print(f"Error replacing header in file {file_path}: {e}")


def update_header(file_path, standard_header, header_pattern):
    """更新文件头部"""
    header_status = check_header(file_path, standard_header, header_pattern)
    if header_status == "correct":
        return
    if header_status == "missing":
        add_header(file_path, standard_header)
    elif header_status == "incorrect_year":
        replace_header(file_path, standard_header, header_pattern)
    elif header_status == "error":
        raise Exception("Error updating header")
    else:
        raise Exception("Unknown header status: {}".format(header_status))


def glob_files(directories, extensions, exclude_dirs=[]):
    """获取文件夹下的文件"""
    files = []
    for directory in directories:
        for root, _, filenames in os.walk(directory):
            if any(exclude_dir in root for exclude_dir in exclude_dirs):
                continue
            for filename in filenames:
                if any(filename.endswith(ext) for ext in extensions):
                    files.append(os.path.join(root, filename))
    return files


def detect_encoding(file_path):
    """检测文件编码"""
    with open(file_path, "rb") as f:
        raw_data = f.read()
        result = chardet.detect(raw_data)
        return result["encoding"]


def check_encoding(files, encodings):
    """检查文件编码"""
    wrong_encoding_files = []
    for file_path in files:
        file_encoding = detect_encoding(file_path)
        if file_encoding not in encodings:
            wrong_encoding_files.append(file_path)
    return wrong_encoding_files


def apply_encoding(files, encoding):
    """应用编码"""
    for file_path in files:
        file_endcoding = detect_encoding(file_path)
        if file_endcoding == encoding:
            continue
        with open(file_path, "r", encoding=file_endcoding) as f:
            content = f.read()
        with open(file_path, "w", encoding=encoding) as f:
            f.write(content)


class CppFormatter:
    standard_header = f"""// ----------------------------------------------------------------------------
// Copyright (c) {HEADER_YEAR_BEGIN}-{HEADER_YEAR_END} DexForce Technology Co., Ltd.
//
// All rights reserved.
// ----------------------------------------------------------------------------
"""
    header_pattern = re.compile(
        r"// -{76}\n"
        r"// Copyright \(c\) (\d{{4}})-(\d{{4}}) DexForce Technology Co\., Ltd\.\n"
        r"//\n"
        r"// All rights reserved\.\n"
        r"// -{76}\n"
    )

    def __init__(self, file_paths):
        self.file_paths = file_paths

    @staticmethod
    def find_clang_format():
        result = subprocess.run(
            ["clang-format", "--version"], capture_output=True, text=True
        )
        if result.returncode != 0:
            raise Exception(
                "Error running clang-format, please make sure clang-format is installed"
            )
        if len(result.stdout.split()) < 3:
            raise Exception("Error parsing clang-format version")
        version = result.stdout.split()[2]
        print(f"clang-format version: {version}")
        required_version = "14.0.0"
        if version < required_version:
            raise Exception(
                f"Please install clang-format version {required_version} or higher"
            )

    @staticmethod
    def _check_style(file_path):
        """Returns (is_valid_style, is_valid_header)"""
        header_status = check_header(
            file_path, CppFormatter.standard_header, CppFormatter.header_pattern
        )
        is_valid_header = header_status == "correct"

        # 用 --dry-run 检测是否需要格式化（兼容不支持某些 key 的旧版 clang-format）
        cmd = [
            "clang-format",
            "--style=file:./.clang-format",
            "--dry-run",
            "--Werror",
            file_path,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            # 退出码 0 表示无需修改；非 0 表示有 diff 或有警告
            is_valid_style = result.returncode == 0
        except Exception:
            is_valid_style = False

        return (is_valid_style, is_valid_header)

    @staticmethod
    def _apply_style(file_path):
        update_header(
            file_path, CppFormatter.standard_header, CppFormatter.header_pattern
        )
        # apply style（容错：旧版 clang-format 遇到不支持的 key 会报错，忽略 stderr 继续格式化）
        cmd = ["clang-format", "--style=file:./.clang-format", "-i", file_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            # 尝试 fallback：用 Google 默认风格格式化
            fallback_cmd = ["clang-format", "--style=Google", "-i", file_path]
            subprocess.run(fallback_cmd, capture_output=True, text=True)
        # 确保文件末尾有换行符（等价于 InsertNewlineAtEOF: true）
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        if content and not content.endswith("\n"):
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content + "\n")

    def run(self, apply, verbose):
        """执行代码风格检查"""
        print(f"Checking C++ style for {len(self.file_paths)} files...")

        if verbose:
            print("To format:")
            for file_path in self.file_paths:
                print("> {}".format(file_path))

        start_time = time.time()
        changed_files = []
        wrong_header_files = []
        for file_path in self.file_paths:
            is_valid_style, is_valid_header = self._check_style(file_path)
            if not is_valid_style:
                changed_files.append(file_path)
            if not is_valid_header:
                wrong_header_files.append(file_path)
        print("Style check takes {:.2f}s".format(time.time() - start_time))

        if changed_files:
            print("代码风格检查未通过，请使用 clang-format 格式化以下文件: ")
            for file in changed_files:
                print(file)

        if wrong_header_files:
            print(f"文件头部声明不符合规范，请在文件头部添加以下声明: \n{CppFormatter.standard_header}")
            print("以下文件头部声明不符合规范: ")
            for file in wrong_header_files:
                print(file)

        if apply:
            print("Applying style...")
            start_time = time.time()
            all_files = changed_files + wrong_header_files
            for file_path in all_files:
                self._apply_style(file_path)
            print("Formatting takes {:.2f}s".format(time.time() - start_time))
            print("Cpp代码风格已应用")
            return True

        if changed_files or wrong_header_files:
            print("Cpp代码风格未通过")
            return False

        print("Cpp代码风格通过")
        return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="通用 C++ 代码风格检查工具")
    parser.add_argument(
        "--apply", dest="apply", action="store_true", default=False,
        help="自动修复格式和版权头",
    )
    parser.add_argument(
        "--verbose", dest="verbose", action="store_true", default=False,
        help="打印所有文件名",
    )
    args = parser.parse_args()

    project_dir = os.path.abspath(os.getcwd())
    print(f"Checking code style for project in {project_dir}")

    CppFormatter.find_clang_format()
    cpp_files = glob_files(
        directories=[os.path.join(project_dir, sub_dir) for sub_dir in CPP_FORMAT_DIRS],
        extensions=[".h", ".cpp", ".hpp"],
        exclude_dirs=[os.path.join(project_dir, sub_dir) for sub_dir in CPP_FORMAT_EXCLUDE_DIRS],
    )
    print(f"Found {len(cpp_files)} C++ files")

    wrong_encoding_files = check_encoding(cpp_files, ["utf-8", "ascii"])
    if wrong_encoding_files:
        print("以下文件编码错误，请使用 utf-8 编码: ")
        for file in wrong_encoding_files:
            print(file)
        if args.apply:
            apply_encoding(wrong_encoding_files, "utf-8")
            print("编码已应用")
        else:
            exit(1)

    # format C++ files

    # format C++ files
    cpp_formatter = CppFormatter(cpp_files)
    if cpp_formatter.run(apply=args.apply, verbose=args.verbose) is False:
        exit(1)
