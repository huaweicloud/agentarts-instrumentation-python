#!/usr/bin/env python3
"""在全新 jiuwenswarm Docker 宿主里跑一次 AgentArts 探针回归。

流程：docker run（纯净宿主）→ docker exec 装探针并注入 → docker restart
→ 校验探针 armed/applied 日志 → HTTP 建会话并发一条消息（读 SSE）。
PASS = 探针日志齐 + SSE 收到非空回答。平台侧入库仍需人工去 AgentArts 控制台确认。

仅依赖标准库与 docker CLI；在宿主机运行。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib import request

DOCKER_DIR = Path(__file__).resolve().parent
DEFAULT_IMAGE = "agentarts/jiuwenswarm-host:dev-stable"
CONTAINER_HTTP_PORT = 19002
CONTAINER_UI_PORT = 5173
# 探针从 GitHub Release 直接安装，不在本地编译；升级版本只改这里。
DEFAULT_PROBE_SPEC = (
    "https://github.com/huaweicloud/agentarts-instrumentation-python/releases/"
    "download/v0.1.0a4/"
    "agentarts_instrumentation_openjiuwen-0.1.0a4-py3-none-any.whl"
)


def log(message: str) -> None:
    print(f"[probe-smoke] {message}", flush=True)


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"docker {' '.join(args)} failed ({result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def wait_health(base_url: str, container: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        running = docker("inspect", "-f", "{{.State.Running}}", container, check=False)
        if running.stdout.strip() != "true":
            log("容器已退出，跳过健康等待")
            return False
        try:
            with request.urlopen(f"{base_url}/api/v1/health", timeout=5) as resp:
                if resp.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(3)
    return False


def create_session(base_url: str) -> str:
    payload = json.dumps(
        {"create_token": f"smoke-{uuid.uuid4().hex}", "mode": "agent"}
    ).encode()
    req = request.Request(
        f"{base_url}/api/v1/sessions",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Request-Id": f"smoke-{uuid.uuid4().hex}",
            "X-User-Id": "probe-smoke",
        },
    )
    with request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode())
    if not body.get("ok"):
        raise RuntimeError(f"session.create failed: {body}")
    return body["data"]["session_id"]


def read_sse(base_url: str, session_id: str, prompt: str, timeout: float) -> str:
    """发一条消息并读到终态，返回回答文本；追问/错误抛异常。"""
    payload = json.dumps(
        {
            "session_id": session_id,
            "query": prompt,
            "content": prompt,
            "mode": "agent",
            "enable_streaming": True,
        }
    ).encode()
    req = request.Request(
        f"{base_url}/api/v1/chat/completions",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "X-Request-Id": f"smoke-{uuid.uuid4().hex}",
            "X-Session-Id": session_id,
            "X-User-Id": "probe-smoke",
        },
    )
    deltas: list[str] = []
    event = ""
    final: str | None = None
    with request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("event:"):
                event = line[6:].strip()
                continue
            if line == "":
                event = ""
                continue
            if not line.startswith("data:"):
                continue
            try:
                data = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if event == "chat.delta" and data.get("content"):
                deltas.append(data["content"])
            elif event == "chat.final":
                final = data.get("content") or ""
                break
            elif event == "chat.error":
                raise RuntimeError(f"chat.error: {data}")
            elif event == "chat.ask_user_question":
                raise RuntimeError(f"模型发起了追问，需人工介入: {data}")
    return (final or "").strip() or "".join(deltas).strip()


def dump_logs(container: str, tail: str = "80") -> None:
    logs = docker("logs", "--tail", tail, container, check=False)
    text = (logs.stdout + logs.stderr).strip()
    if text:
        log(f"容器日志（tail {tail}）:\n{text}")


def probe_state(container: str) -> tuple[bool, int]:
    """读容器日志里的探针状态：armed 出现在进程启动，applied 在宿主 import 观测模块时。"""
    logs = docker("logs", "--tail", "5000", container, check=False)
    text = logs.stdout + logs.stderr
    armed = "AgentArts span exporter injection armed" in text
    applied = sum(
        1
        for line in text.splitlines()
        if "AgentArts span exporter injection applied" in line
    )
    log(f"探针日志：armed={armed}, applied={applied}")
    for line in text.splitlines():
        if "[agentarts-instrumentation-inject]" in line:
            log(f"  {line.strip()}")
    return armed, applied


def write_probe_env(container: str, values: dict[str, str]) -> None:
    """把探针凭据写进宿主自己的 env 文件（~/.jiuwenswarm/config/.env）。

    宿主拉起 jiuwenbox 沙箱子进程时按白名单过滤环境变量，`-e` 传的 token /
    agent_resource_id 不会透传，沙箱里的探针会 skip；写进 env 文件后每个进程
    （含沙箱子进程）都能读到。
    """
    script = (
        "import os, json, re, pathlib\n"
        "vals = {k[len('PROBE_CRED_'):]: v for k, v in os.environ.items()"
        " if k.startswith('PROBE_CRED_')}\n"
        "path = pathlib.Path.home() / '.jiuwenswarm' / 'config' / '.env'\n"
        "path.parent.mkdir(parents=True, exist_ok=True)\n"
        "lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []\n"
        "out, seen = [], set()\n"
        "for line in lines:\n"
        "    m = re.match(r'\\s*([A-Za-z_][A-Za-z0-9_]*)\\s*=', line)\n"
        "    if m and m.group(1) in vals:\n"
        "        out.append(m.group(1) + '=' + json.dumps(vals[m.group(1)]))\n"
        "        seen.add(m.group(1))\n"
        "    else:\n"
        "        out.append(line)\n"
        "out += [k + '=' + json.dumps(v) for k, v in vals.items() if k not in seen]\n"
        "path.write_text('\\n'.join(out) + '\\n', encoding='utf-8')\n"
        "print('[probe-smoke] env file updated:', path, sorted(vals))\n"
    )
    exec_args = ["exec"]
    for key, value in values.items():
        exec_args += ["-e", f"PROBE_CRED_{key}={value}"]
    exec_args += [container, "python", "-c", script]
    log(docker(*exec_args).stdout.strip())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    env = os.environ
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument(
        "--probe-spec",
        default=DEFAULT_PROBE_SPEC,
        help="探针安装源（默认 GitHub Release wheel URL）",
    )
    parser.add_argument("--name", default=f"jiuwenswarm-probe-{uuid.uuid4().hex[:8]}")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--http-port", type=int, default=CONTAINER_HTTP_PORT)
    parser.add_argument("--ui-port", type=int, default=CONTAINER_UI_PORT)
    parser.add_argument("--startup-timeout", type=float, default=300)
    parser.add_argument("--chat-timeout", type=float, default=300)
    parser.add_argument("--prompt", default="请只回复“收到”两个字，不要调用任何工具。")
    parser.add_argument("--keep", action="store_true", help="结束后保留容器")
    parser.add_argument("--build", action="store_true", help="先构建镜像")
    parser.add_argument("--build-arg", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--model-api-base", default=env.get("API_BASE", ""))
    parser.add_argument("--model-api-key", default=env.get("API_KEY", ""))
    parser.add_argument("--model-name", default=env.get("MODEL_NAME", ""))
    parser.add_argument("--model-provider", default=env.get("MODEL_PROVIDER", "OpenAI"))
    parser.add_argument(
        "--agentarts-token", default=env.get("agentarts_agentops_apm_token", "")
    )
    parser.add_argument("--agent-resource-id", default=env.get("agent_resource_id", ""))
    parser.add_argument(
        "--agentarts-endpoint",
        default=env.get("agentarts_agentops_apm_exporter_endpoint", ""),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not shutil.which("docker"):
        log("未找到 docker 命令")
        return 2
    missing = [
        name
        for name, value in (
            ("--model-api-base", args.model_api_base),
            ("--model-api-key", args.model_api_key),
            ("--model-name", args.model_name),
            ("--agentarts-token", args.agentarts_token),
            ("--agent-resource-id", args.agent_resource_id),
        )
        if not value
    ]
    if missing:
        log(f"缺少参数：{', '.join(missing)}")
        return 2

    log(f"探针安装源: {args.probe_spec}")

    if args.build:
        cmd = ["build", "-f", str(DOCKER_DIR / "Dockerfile"), "-t", args.image]
        for item in args.build_arg:
            cmd += ["--build-arg", item]
        docker(*cmd, str(DOCKER_DIR))
        log(f"镜像已构建: {args.image}")

    base_url = f"http://{args.bind}:{args.http_port}"
    container = args.name
    env = {
        "API_BASE": args.model_api_base,
        "API_KEY": args.model_api_key,
        "MODEL_NAME": args.model_name,
        "MODEL_PROVIDER": args.model_provider,
        "agentarts_agentops_apm_token": args.agentarts_token,
        "agent_resource_id": args.agent_resource_id,
    }
    if args.agentarts_endpoint:
        env["agentarts_agentops_apm_exporter_endpoint"] = args.agentarts_endpoint
    env_args: list[str] = []
    for key, value in env.items():
        env_args += ["-e", f"{key}={value}"]

    docker("rm", "-f", container, check=False)
    try:
        docker(
            "run",
            "-d",
            "--init",
            "--name",
            container,
            "-p",
            f"{args.bind}:{args.http_port}:{CONTAINER_HTTP_PORT}",
            "-p",
            f"{args.bind}:{args.ui_port}:{CONTAINER_UI_PORT}",
            *env_args,
            args.image,
        )
        log(f"容器已启动: {container}（基线：未装探针）")

        if not wait_health(base_url, container, args.startup_timeout):
            log("FAIL: 宿主未在超时内就绪")
            dump_logs(container)
            return 1

        docker(
            "exec",
            container,
            "python",
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--no-deps",
            "--force-reinstall",
            args.probe_spec,
        )
        docker("exec", container, "agentarts-instrumentation-inject")
        log("探针已安装（GitHub Release）并注入 .pth")

        probe_env = {
            "agentarts_agentops_apm_token": args.agentarts_token,
            "agent_resource_id": args.agent_resource_id,
        }
        if args.agentarts_endpoint:
            probe_env["agentarts_agentops_apm_exporter_endpoint"] = (
                args.agentarts_endpoint
            )
        write_probe_env(container, probe_env)

        docker("restart", container)
        if not wait_health(base_url, container, args.startup_timeout):
            log("FAIL: 重启后宿主未就绪")
            dump_logs(container)
            return 1

        armed, _ = probe_state(container)
        if not armed:
            log("FAIL: 未看到探针 armed 日志")
            dump_logs(container)
            return 1

        session_id = create_session(base_url)
        log(f"会话已创建: {session_id}")
        answer = read_sse(base_url, session_id, args.prompt, args.chat_timeout)
        if not answer:
            log("FAIL: 对话返回空内容")
            dump_logs(container)
            return 1

        armed, applied = probe_state(container)
        if not (armed and applied >= 1):
            log("FAIL: 对话后仍未看到 applied 日志（宿主可能没初始化 observability）")
            dump_logs(container)
            return 1

        log(f"PASS: 探针布防 + 对话成功（回答 {len(answer)} 字符）")
        return 0
    except Exception as exc:  # noqa: BLE001 - 冒烟脚本统一收敛为 FAIL
        log(f"FAIL: {exc}")
        dump_logs(container)
        return 1
    finally:
        if args.keep:
            log(f"容器保留: {container}（docker logs -f {container}）")
        else:
            docker("rm", "-f", container, check=False)


if __name__ == "__main__":
    sys.exit(main())
