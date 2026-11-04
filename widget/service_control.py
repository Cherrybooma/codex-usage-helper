"""Start/inspect/stop this vault's local read-only API without installing a daemon."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from markdown_store import DEFAULT_DATA, DEFAULT_VAULT

ROOT = Path(__file__).resolve().parent
VAULT = DEFAULT_VAULT
DATA = DEFAULT_DATA


def health(vault, port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        # Loopback traffic must not inherit a global HTTP proxy configuration.
        connection.request("GET", "/health")
        response = connection.getresponse()
        if response.status != 200:
            return {"ready": False}
        result = json.loads(response.read())
        expected = hashlib.sha256(str(Path(vault).resolve()).encode()).hexdigest()[:16]
        if result.get("provider") != "markdown" or result.get("vault_id") != expected:
            return {"ready": False, "error": "port_in_use"}
        return {"ready": True, "url": f"http://127.0.0.1:{port}/api/v1/projects", "health": result["data"]}
    except (OSError, ValueError, http.client.HTTPException):
        return {"ready": False}
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "status", "stop"))
    parser.add_argument("--vault", type=Path, default=VAULT)
    parser.add_argument("--data-dir", type=Path, default=DATA)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("无效端口")
    state_file = args.data_dir / "management-api-process.json"
    current = health(args.vault, args.port)
    if args.action == "status":
        print(json.dumps(current, ensure_ascii=False))
        return 0 if current.get("ready") else 1
    if args.action == "stop":
        try:
            state = json.loads(state_file.read_text())
            pid = int(state["pid"])
            command = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True)
            expected = str(ROOT / "management_api.py")
            if expected not in command or str(args.vault.resolve()) not in command:
                raise ValueError("进程身份与本资料库不一致，未停止。")
            os.kill(pid, signal.SIGTERM)
            state_file.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        except (ProcessLookupError, subprocess.CalledProcessError):
            state_file.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError) as error:
            print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
            return 1
        print(json.dumps({"ok": True, "stopped": True}, ensure_ascii=False))
        return 0
    if current.get("ready"):
        print(json.dumps(current, ensure_ascii=False))
        return 0
    if current.get("error"):
        print(json.dumps(current, ensure_ascii=False))
        return 1
    args.data_dir.mkdir(parents=True, exist_ok=True)
    with (args.data_dir / "management-api.log").open("a") as log:
        process = subprocess.Popen([sys.executable, str(ROOT / "management_api.py"), "--vault", str(args.vault.resolve()),
                                    "--data-dir", str(args.data_dir.resolve()), "--port", str(args.port)],
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   start_new_session=True, cwd=ROOT)
    state_file.write_text(json.dumps({"pid": process.pid, "vault": str(args.vault.resolve()), "port": args.port}) + "\n")
    state_file.chmod(0o600)
    for _ in range(30):
        current = health(args.vault, args.port)
        if current.get("ready"):
            print(json.dumps(current, ensure_ascii=False))
            return 0
        if process.poll() is not None:
            break
        time.sleep(0.1)
    print(json.dumps({"ok": False, "error": "接口启动失败；请检查系统日志或运行系统检查。"}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
