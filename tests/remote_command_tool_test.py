#!/usr/bin/env python3
"""patches/0010 on a host llama-server: the remote_command tool sends the
command to the command server (POST /run, scripts/server_ubuntu26.py) and
gives the model its exit code, stdout and stderr.

Run after scripts/build-fleet-host.sh. A stand-in command server here answers
as the real one does, so no command runs anywhere.
"""
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import argparse
import json
import os
import threading
import urllib.error
import urllib.request

from fleet_rpc_smoke import free_ports, spawn, wait_ready
from make_fleet_fixture import ROOT, make_fixture

TOKEN = "test-token"


class CommandServer(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        CommandServer.received.append((self.path, self.headers.get("X-Command-Token"), body))
        if self.headers.get("X-Command-Token") != TOKEN:
            status, reply = 403, {"stdout": "", "stderr": "Wrong or missing X-Command-Token", "exit_code": 1}
        elif body["command"] == "big":
            status, reply = 200, {"stdout": "x" * 20000, "stderr": "", "exit_code": 0}
        else:
            status, reply = 200, {"stdout": "hello \"world\"\n", "stderr": "warn\n", "exit_code": 3}
        data = json.dumps(reply).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def request(port, method, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}/tools", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")


def run_tool(port, command, **params):
    return request(port, "POST", {"tool": "remote_command", "params": {"command": command, **params}})


def run(build, output):
    output.mkdir(parents=True, exist_ok=True)
    fixture = make_fixture(output / "tiny-llama.gguf")
    server = build / "bin/llama-server"
    if not server.is_file():
        raise FileNotFoundError(f"{server}; first run scripts/build-fleet-host.sh")
    port, command_port, dead_port = free_ports(3)
    command = [str(server), "--model", str(fixture), "--host", "127.0.0.1", "--port", str(port),
               "--ctx-size", "256", "--parallel", "1", "--threads", "2", "--device", "none",
               "--gpu-layers", "0", "--fit", "off", "--no-warmup", "--tools", "read_file,remote_command"]
    commands = HTTPServer(("127.0.0.1", command_port), CommandServer)
    threading.Thread(target=commands.serve_forever, daemon=True).start()
    with ExitStack() as stack:
        stack.callback(commands.server_close)
        stack.callback(commands.shutdown)
        env = dict(os.environ, LLAMA_REMOTE_COMMAND=f"127.0.0.1:{command_port}", LLAMA_REMOTE_COMMAND_TOKEN=TOKEN)
        process = spawn(stack, command, output / "server.log", env)
        wait_ready(process, port, http=True)

        # Listed for the web UI, as a tool that asks before it runs.
        status, tools = request(port, "GET")
        assert status == 200, tools
        tool = next(t for t in tools if t["tool"] == "remote_command")
        assert tool["permissions"]["write"] is True, tool
        assert tool["definition"]["function"]["parameters"]["required"] == ["command"], tool

        # The command, its timeout and the token reach POST /run; the answer reaches the model.
        status, body = run_tool(port, 'echo "hi"')
        assert status == 200, body
        assert CommandServer.received[-1] == ("/run", TOKEN, {"command": 'echo "hi"', "timeout": 30}), CommandServer.received
        assert body["plain_text_response"] == '[exit code: 3]\nstdout:\nhello "world"\nstderr:\nwarn\n', body

        # The timeout is capped at 120 seconds.
        run_tool(port, "true", timeout=999)
        assert CommandServer.received[-1][2]["timeout"] == 120, CommandServer.received[-1]

        # Long output is cut, as exec_shell_command cuts it.
        status, body = run_tool(port, "big")
        assert status == 200, body
        assert "[cut: 20000 bytes in all]" in body["plain_text_response"], body["plain_text_response"][-80:]
        assert len(body["plain_text_response"]) < 17000

    # A wrong token: the server's refusal is what the model sees.
    # No command server: an error, not a hang.
    with ExitStack() as stack:
        commands = HTTPServer(("127.0.0.1", command_port), CommandServer)
        threading.Thread(target=commands.serve_forever, daemon=True).start()
        stack.callback(commands.server_close)
        stack.callback(commands.shutdown)
        port2 = free_ports(1)[0]
        env = dict(os.environ, LLAMA_REMOTE_COMMAND=f"127.0.0.1:{command_port}", LLAMA_REMOTE_COMMAND_TOKEN="wrong")
        command[command.index("--port") + 1] = str(port2)
        process = spawn(stack, command, output / "server-wrong-token.log", env)
        wait_ready(process, port2, http=True)
        status, body = run_tool(port2, "ls")
        assert status == 200, body
        assert "Wrong or missing X-Command-Token" in body["plain_text_response"], body

        port3 = free_ports(1)[0]
        env = dict(os.environ, LLAMA_REMOTE_COMMAND=f"127.0.0.1:{dead_port}")
        command[command.index("--port") + 1] = str(port3)
        process = spawn(stack, command, output / "server-no-command-server.log", env)
        wait_ready(process, port3, http=True)
        status, body = run_tool(port3, "ls")
        assert status == 500 and "no answer from" in body, (status, body)
    print("remote_command: command, timeout and token sent; output, cut and errors returned")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, default=ROOT / "build/fleet-host")
    parser.add_argument("--output", type=Path, default=ROOT / "build/remote-command-tool")
    args = parser.parse_args()
    run(args.build.resolve(), args.output.resolve())
