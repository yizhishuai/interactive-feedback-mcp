# Interactive Feedback MCP
# Developed by Fábio Ferreira (https://x.com/fabiomlferreira)
# Inspired by/related to dotcursorrules.com (https://dotcursorrules.com/)
import os
import sys
import json
import asyncio
import tempfile
import subprocess

from typing import Annotated, Dict

import psutil
from fastmcp import FastMCP
from pydantic import Field

# Version identifier for debugging
SERVER_VERSION = "v0.1.3-caller-detect"

# The log_level is necessary for Cline to work: https://github.com/jlowin/fastmcp/issues/81
mcp = FastMCP("Interactive Feedback MCP")

# Timeout configuration
CLI_AGENT_TIMEOUT_SECONDS = int(os.getenv("INTERACTIVE_FEEDBACK_CLI_TIMEOUT_SECONDS", "60"))
CHAT_TIMEOUT_SECONDS = int(os.getenv("INTERACTIVE_FEEDBACK_TIMEOUT_SECONDS", "290"))


def detect_caller_mode() -> str:
    """检测 MCP 调用来源：cursor_chat 或 cursor_cli

    通过遍历父进程链来判断：
    - Cursor Chat (GUI): 父进程链中包含 Cursor Electron 主进程 (Cursor Helper, Electron 等)
    - Cursor CLI Agent: 父进程链中包含 cursor CLI 命令 (通常带 agent 子命令)

    Returns:
        "cursor_chat" | "cursor_cli" | "unknown"
    """
    try:
        p = psutil.Process(os.getpid())
        parent = p.parent()
        process_chain = []

        while parent:
            try:
                name = parent.name().lower()
                cmdline_parts = parent.cmdline()
                cmdline = " ".join(cmdline_parts[:5]).lower()
                process_chain.append((name, cmdline))
                parent = parent.parent()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                break

        # 输出进程链日志用于调试
        print("[INFO] Process chain for caller detection:", file=sys.stderr)
        for name, cmdline in process_chain:
            print(f"  [{name}] {cmdline}", file=sys.stderr)

        # 检测逻辑
        for name, cmdline in process_chain:
            # CLI Agent 特征：cursor CLI 进程，通常命令行中包含 agent 相关参数
            # macOS: /usr/local/bin/cursor 或类似路径
            # 关键区分：CLI 是独立的命令行进程，不是 Electron renderer
            if name == "cursor" and ("agent" in cmdline or "--stdio" not in cmdline):
                # 进一步确认：CLI 的上层应该是 shell（bash/zsh/fish 等），而不是 Electron
                continue  # 先收集完整链再判断

            # Cursor GUI (Electron) 特征
            if "cursor helper" in name or "cursor helper" in cmdline:
                return "cursor_chat"
            if name == "electron" or "electron" in cmdline:
                return "cursor_chat"
            # macOS 上 Cursor.app 的主进程
            if "cursor.app" in cmdline:
                return "cursor_chat"
            # Cursor 主进程（非 CLI）
            if name == "cursor" and (".app" in cmdline or "electron" in cmdline):
                return "cursor_chat"

        # 第二轮：检测 CLI Agent
        for name, cmdline in process_chain:
            # cursor CLI 通常是一个独立二进制，父进程是 shell
            if name == "cursor" and "agent" in cmdline:
                return "cursor_cli"
            # 有些情况下 CLI 进程名可能是 cursor-cli 或包含 cli 关键字
            if "cursor" in name and "cli" in name:
                return "cursor_cli"
            if "cursor" in name and "cli" in cmdline:
                return "cursor_cli"

        # 第三轮：通过 shell 父进程间接判断
        # 如果进程链中有 cursor 进程，且其父进程是 shell（bash/zsh/fish），大概率是 CLI
        for i, (name, cmdline) in enumerate(process_chain):
            if "cursor" in name:
                # 检查它的父进程（链中下一个）是否是 shell
                if i + 1 < len(process_chain):
                    parent_name = process_chain[i + 1][0]
                    if parent_name in ("bash", "zsh", "fish", "sh", "dash", "tcsh"):
                        return "cursor_cli"
                # 如果 cursor 是链中最顶层进程，也可能是 CLI
                return "cursor_cli"

        return "unknown"

    except Exception as e:
        print(f"[WARN] Failed to detect caller mode: {e}", file=sys.stderr)
        return "unknown"


def get_timeout_for_caller() -> int:
    """根据调用来源返回合适的超时时间"""
    mode = detect_caller_mode()
    if mode == "cursor_cli":
        timeout = CLI_AGENT_TIMEOUT_SECONDS
    elif mode == "cursor_chat":
        timeout = CHAT_TIMEOUT_SECONDS
    else:
        # 未知来源，使用 Chat 的默认值（更保守）
        timeout = CHAT_TIMEOUT_SECONDS
    print(f"[INFO] Caller mode: {mode}, timeout: {timeout}s", file=sys.stderr)
    return timeout


# Log version on startup
print(f"[INFO] Interactive Feedback MCP {SERVER_VERSION} starting...", file=sys.stderr)
print(f"[INFO] CLI timeout: {CLI_AGENT_TIMEOUT_SECONDS}s, Chat timeout: {CHAT_TIMEOUT_SECONDS}s", file=sys.stderr)


def _cleanup_process(proc: subprocess.Popen | None) -> None:
    """安全地终止子进程"""
    if proc is None:
        return
    if proc.poll() is None:  # 进程仍在运行
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _cleanup_file(file_path: str) -> None:
    """安全地删除临时文件"""
    try:
        if os.path.exists(file_path):
            os.unlink(file_path)
    except OSError:
        pass  # 忽略删除失败的情况


async def launch_feedback_ui_async(
    project_directory: str, summary: str, task_id: str, timeout_seconds: int = 290
) -> dict[str, str]:
    """异步启动反馈UI并等待结果，不阻塞MCP服务器的其他请求

    正确处理请求取消：当 MCP 客户端取消请求时，会抛出 asyncio.CancelledError，
    我们需要捕获它，清理资源，然后重新抛出让 FastMCP 正确处理。
    """
    # Create a temporary file for the feedback result
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        output_file = tmp.name

    proc: subprocess.Popen | None = None

    try:
        # Get the absolute path to feedback_ui.py
        script_dir = os.path.dirname(os.path.abspath(__file__))
        feedback_ui_path = os.path.abspath(os.path.join(script_dir, "feedback_ui.py"))

        # Ensure the path exists
        if not os.path.exists(feedback_ui_path):
            raise Exception(f"feedback_ui.py not found at: {feedback_ui_path}")

        # Run feedback_ui.py as a separate process
        # NOTE: There appears to be a bug in uv, so we need
        # to pass a bunch of special flags to make this work
        # Try to find the correct python executable
        python_exe = sys.executable

        # Check if we're in a virtual environment and if so, use the venv python
        if hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix:
            venv_python = os.path.join(sys.prefix, "bin", "python")
            if os.path.exists(venv_python):
                python_exe = venv_python

        args = [
            python_exe,
            "-u",
            feedback_ui_path,
            "--project-directory",
            project_directory,
            "--prompt",
            summary,
            "--output-file",
            output_file,
            "--timeout-seconds",
            str(timeout_seconds),
            "--task-id",
            task_id,
        ]
        # Start the subprocess
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            cwd=project_directory,  # Run in project directory
        )

        # 异步非阻塞等待：在等待UI响应期间，事件循环可以处理其他MCP请求
        while proc.poll() is None:
            # Check if output file exists and has content
            if os.path.exists(output_file) and os.path.getsize(output_file) > 0:
                try:
                    with open(output_file, "r") as f:
                        result = json.load(f)
                    _cleanup_process(proc)
                    _cleanup_file(output_file)
                    return result
                except (json.JSONDecodeError, OSError):
                    # File exists but not ready yet, continue waiting
                    pass

            # 关键：使用异步sleep，这里可能抛出 CancelledError
            await asyncio.sleep(0.1)

        # Process completed, read the result
        if proc.returncode != 0:
            raise Exception(f"Failed to launch feedback UI: {proc.returncode}")

        # Read the result from the temporary file
        with open(output_file, "r") as f:
            result = json.load(f)
        _cleanup_file(output_file)
        return result

    except asyncio.CancelledError:
        # MCP 请求被取消（用户在 Cursor 中取消、超时等）
        # 必须清理资源，然后重新抛出让 FastMCP 正确处理
        # 不要返回任何响应，否则会导致 "unknown message ID" 错误
        print(
            "[INFO] Request cancelled, cleaning up subprocess and temp file",
            file=sys.stderr,
        )
        _cleanup_process(proc)
        _cleanup_file(output_file)
        raise  # 重新抛出 CancelledError，让 FastMCP 处理

    except Exception as e:
        _cleanup_process(proc)
        _cleanup_file(output_file)
        raise e


def first_line(text: str) -> str:
    return text.split("\n")[0].strip()


@mcp.tool()
async def interactive_feedback(
    project_directory: Annotated[
        str, Field(description="Full path to the project directory")
    ],
    summary: Annotated[
        str,
        Field(
            description="Brief one-line summary of changes or question to ask the user"
        ),
    ],
    task_id: Annotated[
        str,
        Field(description="Task identifier to distinguish different tasks (required)"),
    ],
) -> Dict[str, str]:
    """Interactive Feedback Tool for MCP (Model Context Protocol)

    This tool enables AI assistants to request real-time feedback from users during coding sessions.
    It opens an interactive feedback window where users can provide input, ask questions, or give directions.

    Parameters:
    - project_directory: Full path to the project directory being worked on
    - summary: Brief one-line summary of changes made, or a specific question to ask the user
    - task_id: Task identifier to distinguish different tasks (required)

    Usage:
    - Use this tool whenever you need user input, clarification, or approval for your work
    - The tool opens a GUI window for user interaction and returns their response
    - Keep calling this tool to maintain continuous dialogue until user says "end conversation"

    Important Rules:
    - Always call this tool after completing any work or response
    - Never end conversation unless user explicitly says "end conversation"
    - Use summary parameter for brief updates or specific questions to the user
    - Use task_id parameter to help users distinguish between different tasks
    - Maintain continuous dialogue by repeatedly calling this tool

    """
    timeout = get_timeout_for_caller()
    return await launch_feedback_ui_async(
        first_line(project_directory),
        summary,
        first_line(task_id),
        timeout,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
