import argparse
import os
import re
import subprocess
import time

import chardet

CPP_FORMAT_DIRS = [
    "src",
    "camera",
]
CPP_FORMAT_EXCLUDE_DIRS = [
    os.path.join("src", "3rd-party"),
    os.path.join("src", "protobuf"),
    os.path.join("camera", "third_party"),
]

HEADER_YEAR_BEGIN = 2021
HEADER_YEAR_END = 2025


def check_header(file_path, standard_header, header_pattern):
    """
    检查文件头部是否缺少标准头部，或者存在头部但年份错误

    Args:
        file_path (str): 文件路径
        standard_header (str): 标准头部字符串
        header_pattern (str): 用于匹配头部的正则表达式

    Returns:
        str: 头部状态，可能的值有 "missing", "correct", "incorrect_year", "error"
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            if not content.startswith(standard_header):
                # 检查是否存在头部但年份错误
                match = re.match(header_pattern, content)
                if match:
                    start_year, end_year = match.groups()
                    if (
                        int(start_year) != HEADER_YEAR_BEGIN
                        or int(end_year) != HEADER_YEAR_END
                    ):
                        # 存在头部但年份错误
                        return "incorrect_year"
                else:
                    # 缺少标准头部
                    return "missing"
            else:
                # 标准头部存在且正确
                return "correct"
    except Exception as e:
        print(f"Error reading file {file_path}: {e}")
        return "error"


def add_header(file_path, header):
    """
    向文件添加头部

    Args:
        file_path (str): 文件路径
        header (str): 头部内容
    """
    try:
        with open(file_path, "r+", encoding="utf-8") as f:
            content = f.read()
            f.seek(0, 0)
            f.write(header + "\n" + content)
    except Exception as e:
        print(f"Error adding header to file {file_path}: {e}")


def replace_header(file_path, header, header_pattern):
    """
    替换文件头部

    Args:
        file_path (str): 文件路径
        header (str): 头部内容
        header_pattern (str): 用于匹配头部的正则表达式
    """
    try:
        with open(file_path, "r+", encoding="utf-8") as f:
            content = f.read()
            new_content = re.sub(header_pattern, header, content)
            f.seek(0, 0)
            f.write(new_content)
    except Exception as e:
        print(f"Error replacing header in file {file_path}: {e}")


def update_header(file_path, standard_header, header_pattern):
    """
    更新文件头部

    Args:
        file_path (str): 文件路径
        standard_header (str): 标准头部字符串
        header_pattern (str): 用于匹配头部的正则表达式
    """
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
    """获取文件夹下的文件

    Args:
        directories (str): 文件夹路径
        extensions (list): 文件扩展名列表
        exclude_dirs (list): 排除的文件夹列表

    Returns:
        list: 文件列表
    """
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
    """
    检测文件的编码

    Args:
        file_path (str): 文件路径

    Returns:
        str: 文件编码
    """
    with open(file_path, "rb") as f:
        raw_data = f.read()
        result = chardet.detect(raw_data)
        return result["encoding"]


def check_encoding(files, encodings):
    """检查文件编码

    Args:
        files (list): 文件列表
        encodings (list): 支持的编码列表

    Returns:
        list: 编码错误的文件列表
    """
    wrong_encoding_files = []
    for file_path in files:
        file_encoding = detect_encoding(file_path)
        if file_encoding not in encodings:
            wrong_encoding_files.append(file_path)
    return wrong_encoding_files


def apply_encoding(files, encoding):
    """应用编码

    Args:
        files (list): 文件列表
        encoding (str): 编码
    """
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
        r"// ----------------------------------------------------------------------------\n"
        r"// Copyright \(c\) (\d{4})-(\d{4}) DexForce Technology Co., Ltd\.\n"
        r"//\n"
        r"// All rights reserved\.\n"
        r"// ----------------------------------------------------------------------------\n"
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
        required_version = "18.1.4"
        if version < required_version:
            raise Exception(
                f"Please install clang-format version {required_version} or higher"
            )

    @staticmethod
    def _check_style(file_path):
        """
        Returns (true, true) if (style, header) is valid.
        """
        header_status = check_header(
            file_path, CppFormatter.standard_header, CppFormatter.header_pattern
        )
        is_valid_header = header_status == "correct"

        cmd = [
            "clang-format",
            "--style=file:./.clang-format",
            "--output-replacements-xml",
            file_path,
        ]
        result = subprocess.check_output(cmd).decode("utf-8")
        if "<replacement " in result:
            is_valid_style = False
        else:
            is_valid_style = True

        return (is_valid_style, is_valid_header)

    @staticmethod
    def _apply_style(file_path):
        update_header(
            file_path, CppFormatter.standard_header, CppFormatter.header_pattern
        )
        # apply style
        cmd = [
            "clang-format",
            "--style=file:./.clang-format",
            "-i",
            file_path,
        ]
        subprocess.check_output(cmd)

    def run(self, apply, verbose):
        """执行代码风格检查

        Args:
            apply (bool): 是否应用代码风格
            verbose (bool): 是否打印文件名

        Returns:
            bool: 是否成功，成功条件如下：
                - apply 为 False 时，所有文件风格正确
                - apply 为 True 时，所有文件风格正确且已应用风格
        """
        print(f"Checking C++ style for {len(self.file_paths)} files...")

        if verbose:
            print("To format:")
            for file_path in self.file_paths:
                print("> {}".format(file_path))

        start_time = time.time()
        is_valid_files = map(CppFormatter._check_style, self.file_paths)
        changed_files = []
        wrong_header_files = []
        for is_valid, file_path in zip(is_valid_files, self.file_paths):
            is_valid_style = is_valid[0]
            is_valid_header = is_valid[1]
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
            print(
                f"文件头部声明不符合规范，请在文件头部添加以下声明: \n{CppFormatter.standard_header}"
            )
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
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        dest="apply",
        action="store_true",
        default=False,
        help="Apply style to files in-place.",
    )
    parser.add_argument(
        "--verbose",
        dest="verbose",
        action="store_true",
        default=False,
        help="If true, prints file names while formatting.",
    )
    args = parser.parse_args()

    project_dir = os.path.abspath(os.getcwd())
    print(f"Checking code style for project in {project_dir}")

    # process C++ files
    CppFormatter.find_clang_format()
    cpp_files = glob_files(
        directories=[os.path.join(project_dir, sub_dir) for sub_dir in CPP_FORMAT_DIRS],
        extensions=[".h", ".cpp", ".hpp"],
        exclude_dirs=[
            os.path.join(project_dir, sub_dir) for sub_dir in CPP_FORMAT_EXCLUDE_DIRS
        ],
    )
    print(f"Found {len(cpp_files)} C++ files")

    # check encoding
    # https://stackoverflow.com/questions/19652939/why-does-chardet-say-my-utf-8-encoded-string-originally-decoded-from-iso-8859-1
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
    cpp_formatter = CppFormatter(cpp_files)
    if cpp_formatter.run(apply=args.apply, verbose=args.verbose) is False:
        exit(1)
