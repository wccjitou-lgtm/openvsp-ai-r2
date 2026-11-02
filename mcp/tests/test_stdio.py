#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""End-to-end stdio tests for the OpenVSP MCP server.

The test starts ``openvsp_mcp_server.py`` as a subprocess, talks JSON-RPC 2.0
over newline-delimited stdin/stdout and verifies that stdout stays protocol
clean while the OpenVSP Python API is exercised.

Run:
    python tests/test_stdio.py                 # fast suite (~15 s)
    python tests/test_stdio.py --full          # also runs VSPAERO / report / worker
    python tests/test_stdio.py -v              # verbose unittest output

Environment:
    OPENVSP_ROOT         OpenVSP install root (default D:\\Zcoding\\OpenVSP-3.51.3-win64)
    OPENVSP_PYTHON       interpreter used by the server (default sys.executable)
    OPENVSP_MCP_WORKDIR  directory for generated files (default tests/_workdir)
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
MCP_DIR = HERE.parent
SERVER = MCP_DIR / "openvsp_mcp_server.py"
# Unique per test process so concurrent runs (or an interrupted run) cannot
# interfere with each other; OPENVSP_MCP_WORKDIR overrides the location.
WORKDIR = Path(os.environ.get("OPENVSP_MCP_WORKDIR")
               or (HERE / ("_workdir_%d" % os.getpid()))).resolve()

DEFAULT_ROOT = r"<OPENVSP_ROOT>"
OPENVSP_ROOT = os.environ.get("OPENVSP_ROOT") or DEFAULT_ROOT
PYTHON = os.environ.get("OPENVSP_PYTHON") or sys.executable

FULL = [False]
TOOL_TIMEOUT = 300.0
LONG_TIMEOUT = 900.0
KEEP_FILES = os.environ.get("OPENVSP_MCP_KEEP_TEST_FILES") == "1"


class McpProcess:
    """Minimal MCP stdio client used by the tests."""

    def __init__(self, extra_env=None, workdir=None):
        env = os.environ.copy()
        env.update({
            "OPENVSP_ROOT": OPENVSP_ROOT,
            "OPENVSP_PYTHON": PYTHON,
            "OPENVSP_MCP_WORKDIR": str(workdir or WORKDIR),
            "OPENVSP_MCP_LOG_LEVEL": "INFO",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        })
        if extra_env:
            env.update({k: str(v) for k, v in extra_env.items()})
        self.proc = subprocess.Popen(
            [PYTHON, "-X", "utf8", str(SERVER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(MCP_DIR), env=env,
        )
        self._queue = queue.Queue()
        self._stderr = []
        self.raw_stdout = []
        self._next_id = 0
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._err_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._err_reader.start()

    # -- transport -------------------------------------------------------
    def _read_stdout(self):
        try:
            for line in self.proc.stdout:
                raw = line.decode("utf-8", errors="replace").strip()
                if not raw:
                    continue
                try:
                    self._queue.put(json.loads(raw))
                except json.JSONDecodeError:
                    self.raw_stdout.append(raw)
                    self._queue.put({"__invalid_json__": raw})
        except (ValueError, OSError):
            pass

    def _read_stderr(self):
        try:
            for line in self.proc.stderr:
                self._stderr.append(line.decode("utf-8", errors="replace").rstrip())
                if len(self._stderr) > 400:
                    del self._stderr[:100]
        except (ValueError, OSError):
            pass

    def send_raw(self, data: bytes):
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def send_json(self, obj):
        self.send_raw(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")

    def notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.send_json(message)

    def request_raw(self, message_id, method, params=None):
        message = {"jsonrpc": "2.0", "id": message_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send_json(message)

    def request(self, method, params=None, timeout=TOOL_TIMEOUT):
        self._next_id += 1
        message_id = self._next_id
        self.request_raw(message_id, method, params)
        return self.wait_for_id(message_id, timeout)

    def wait_for_id(self, message_id, timeout=TOOL_TIMEOUT):
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise AssertionError(
                    "等待响应 id=%r 超时（%.0fs）。stderr:\n%s"
                    % (message_id, timeout, self.stderr_tail()))
            try:
                item = self._queue.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise AssertionError(
                        "服务器进程已退出（code=%s）。stderr:\n%s"
                        % (self.proc.returncode, self.stderr_tail()))
                continue
            if item.get("id") == message_id:
                return item
            # unexpected message: remember for debugging but keep waiting
            self.stderr_tail()

    def wait_any(self, timeout=5.0):
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def assert_silent(self, timeout=0.6):
        item = self.wait_any(timeout)
        if item is not None:
            raise AssertionError("收到非预期消息: %r" % (item,))

    def stderr_tail(self, lines=25):
        return "\n".join(self._stderr[-lines:])

    def tool_call(self, name, arguments=None, timeout=TOOL_TIMEOUT):
        response = self.request("tools/call",
                                {"name": name, "arguments": arguments or {}},
                                timeout=timeout)
        if "error" in response:
            raise AssertionError("tools/call %s 返回协议错误: %r" % (name, response["error"]))
        result = response.get("result") or {}
        content = result.get("content") or []
        text = content[0].get("text") if content else ""
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            raise AssertionError("tools/call %s 返回文本不是 JSON: %r" % (name, text[:500]))
        return result, payload

    # -- lifecycle -------------------------------------------------------
    def close(self):
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        try:
            self._reader.join(timeout=2)
            self._err_reader.join(timeout=2)
        except RuntimeError:
            pass
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass


def _base_env(extra=None):
    env = {
        "OPENVSP_ROOT": OPENVSP_ROOT,
        "OPENVSP_PYTHON": PYTHON,
        "OPENVSP_MCP_WORKDIR": str(WORKDIR),
    }
    if extra:
        env.update(extra)
    return env


class StdioServerTest(unittest.TestCase):
    client = None

    # ------------------------------------------------------------------
    @classmethod
    def setUpClass(cls):
        if cls is not StdioServerTest:
            return
        if WORKDIR.exists():
            shutil.rmtree(WORKDIR, ignore_errors=True)
        WORKDIR.mkdir(parents=True, exist_ok=True)
        cls.client = McpProcess(workdir=WORKDIR)

    @classmethod
    def tearDownClass(cls):
        if cls is not StdioServerTest:
            return
        if cls.client is not None:
            cls.client.close()
            cls.client = None
        if not KEEP_FILES:
            shutil.rmtree(WORKDIR, ignore_errors=True)

    def _client(self):
        if self.client is None:
            raise unittest.SkipTest("共享服务器客户端未初始化")
        return self.client

    # -- 01 initialize --------------------------------------------------
    def test_01_initialize(self):
        client = self._client()
        response = client.request("initialize", {
            "protocolVersion": "2025-11-25",
            "capabilities": {"roots": {"listChanged": True}},
            "clientInfo": {"name": "openvsp-mcp-test", "version": "0.1.0"},
        }, timeout=60)
        self.assertIn("result", response, response)
        result = response["result"]
        self.assertEqual(result.get("protocolVersion"), "2025-11-25")
        self.assertIn("capabilities", result)
        self.assertIn("tools", result["capabilities"])
        server_info = result.get("serverInfo") or {}
        self.assertEqual(server_info.get("name"), "openvsp-mcp")
        self.assertTrue(server_info.get("version"))
        self.assertTrue(result.get("instructions"))
        self.assertEqual(client.raw_stdout, [], "stdout 出现非 JSON 行: %r" % client.raw_stdout[:5])
        client.notify("notifications/initialized")
        client.assert_silent(0.5)

    # -- 02 ping / notification handling --------------------------------
    def test_02_ping_and_notifications(self):
        client = self._client()
        response = client.request("ping")
        self.assertEqual(response.get("result"), {})
        client.notify("notifications/cancelled", {"requestId": 999, "reason": "test"})
        client.notify("notifications/whatever")
        client.assert_silent(0.5)
        response = client.request("ping")
        self.assertEqual(response.get("result"), {})

    # -- 03 tools/list ---------------------------------------------------
    def test_03_tools_list(self):
        client = self._client()
        response = client.request("tools/list", {})
        result = response["result"]
        tools = result.get("tools")
        self.assertIsInstance(tools, list)
        names = {tool["name"] for tool in tools}
        expected = {
            "openvsp_status", "openvsp_new_model", "openvsp_build_aircraft",
            "openvsp_get_geometry", "openvsp_run_vspaero", "openvsp_export",
            "openvsp_generate_report", "openvsp_list_files",
        }
        self.assertEqual(names, expected)
        for tool in tools:
            self.assertIsInstance(tool.get("description"), str)
            schema = tool.get("inputSchema")
            self.assertIsInstance(schema, dict)
            self.assertEqual(schema.get("type"), "object")
            self.assertIn("properties", schema)

    # -- 04 openvsp_status ----------------------------------------------
    def test_04_status(self):
        client = self._client()
        result, payload = client.tool_call("openvsp_status", {})
        self.assertFalse(result.get("isError"))
        self.assertTrue(payload.get("ok"))
        openvsp = payload.get("openvsp") or {}
        self.assertTrue(openvsp.get("available"), payload)
        self.assertIn("3.51.3", str(openvsp.get("version")))
        self.assertTrue(openvsp.get("root_exists"))
        self.assertTrue(openvsp.get("vsp_exe"))
        self.assertTrue(openvsp.get("vspaero_exe"))
        self.assertTrue(openvsp.get("vspaero_available"))
        protocol = payload.get("protocol") or {}
        self.assertEqual(protocol.get("protocolVersion"), "2025-11-25")
        self.assertFalse(protocol.get("modernEraSupported"))
        self.assertEqual(client.raw_stdout, [], "stdout 出现非 JSON 行: %r" % client.raw_stdout[:5])

    # -- 05 new model ----------------------------------------------------
    def test_051_no_model_error(self):
        client = self._client()
        result, payload = client.tool_call("openvsp_get_geometry", {})
        self.assertTrue(result.get("isError"), "空模型时 openvsp_get_geometry 应返回 isError")
        self.assertFalse(payload.get("ok"))
        self.assertEqual((payload.get("error") or {}).get("code"), "no_model")
        result, payload = client.tool_call("openvsp_export", {"format": "vsp3"})
        self.assertTrue(result.get("isError"))
        self.assertEqual((payload.get("error") or {}).get("code"), "no_model")

    # -- 05 new model ----------------------------------------------------
    def test_05_new_model(self):
        client = self._client()
        result, payload = client.tool_call("openvsp_new_model", {"name": "test_uav"})
        self.assertFalse(result.get("isError"))
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload.get("model_name"), "test_uav")

    # -- 06 build aircraft (50 kg class simplified model) ----------------
    def test_06_build_aircraft(self):
        client = self._client()
        spec = {
            "name": "test_uav_50kg",
            "S_ref": 1.2,          # m^2
            "AR": 8.0,
            "taper": 0.7,
            "sweep": 2.0,
            "dihedral": 3.0,
            "cant": 0.0,
            "v_tail": True,
            "Sh": 0.18,
            "Vv": 0.05,
            "engine": "electric",
            "fuselage_len": 1.4,
        }
        result, payload = client.tool_call("openvsp_build_aircraft", {"spec": spec},
                                           timeout=TOOL_TIMEOUT)
        self.assertFalse(result.get("isError"), payload)
        self.assertTrue(payload.get("ok"), payload)
        derived = payload.get("derived") or {}
        self.assertAlmostEqual(derived.get("span_m"), 3.098387, places=3)
        self.assertGreaterEqual(payload.get("geometry_count"), 4)
        names = {item["name"] for item in payload.get("geometries") or []}
        self.assertIn("Wing", names)
        self.assertIn("HTail", names)
        self.assertIn("VTail", names)
        self.assertIn("Fuselage", names)
        refs = payload.get("reference") or {}
        self.assertAlmostEqual(refs.get("S_ref"), 1.2, places=3)
        vsp3 = payload.get("vsp3_path")
        self.assertTrue(vsp3 and Path(vsp3).is_file(), vsp3)
        self.assertGreater(Path(vsp3).stat().st_size, 10_000)

    # -- 07 get geometry -------------------------------------------------
    def test_07_get_geometry(self):
        client = self._client()
        result, payload = client.tool_call("openvsp_get_geometry", {})
        self.assertFalse(result.get("isError"), payload)
        self.assertTrue(payload.get("ok"))
        geometries = payload.get("geometries") or []
        self.assertGreaterEqual(len(geometries), 4)
        wing = next(item for item in geometries if item["name"] == "Wing")
        self.assertEqual(wing["type"], "Wing")
        params = wing.get("params") or {}
        self.assertAlmostEqual(params.get("span_m"), 3.098387, places=3)
        self.assertAlmostEqual(params.get("area_m2"), 1.2, places=3)
        self.assertAlmostEqual(params.get("aspect_ratio"), 8.0, delta=0.1)
        fuselage = next(item for item in geometries if item["name"] == "Fuselage")
        self.assertAlmostEqual(fuselage["params"].get("length_m"), 1.4, places=3)
        summary = payload.get("summary") or {}
        self.assertEqual(summary.get("geometry_count"), len(geometries))
        self.assertIsInstance(summary.get("bounding_box"), dict)

    # -- 08 export -------------------------------------------------------
    def test_08_export(self):
        client = self._client()
        exports = WORKDIR / "exports"
        for fmt in ("vsp3", "stl", "csv"):
            target = exports / ("test_uav_50kg." + fmt)
            result, payload = client.tool_call(
                "openvsp_export", {"format": fmt, "output_path": str(target)})
            self.assertFalse(result.get("isError"), payload)
            self.assertTrue(payload.get("ok"), payload)
            path = Path(payload["output_path"])
            self.assertTrue(path.is_file(), path)
            self.assertGreater(path.stat().st_size, 1000)
            self.assertEqual(path.suffix.lower(), "." + fmt)

    # -- 09 list files ---------------------------------------------------
    def test_09_list_files(self):
        client = self._client()
        result, payload = client.tool_call(
            "openvsp_list_files", {"dir": str(WORKDIR), "recursive": True})
        self.assertFalse(result.get("isError"), payload)
        self.assertTrue(payload.get("ok"))
        entries = payload.get("entries") or []
        names = {entry["name"] for entry in entries}
        self.assertTrue(any(name.endswith(".vsp3") for name in names), names)
        self.assertGreaterEqual(payload.get("entry_count"), 1)

    # -- 10 protocol errors ---------------------------------------------
    def test_10_unknown_tool(self):
        client = self._client()
        response = client.request("tools/call", {"name": "openvsp_does_not_exist",
                                                 "arguments": {}}, timeout=60)
        self.assertIn("error", response)
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("available_tools", response["error"].get("data") or {})

    def test_11_invalid_params(self):
        client = self._client()
        response = client.request("tools/call", {"arguments": {}}, timeout=60)
        self.assertIn("error", response)
        self.assertEqual(response["error"]["code"], -32602)
        response = client.request("tools/call", {"name": "openvsp_status",
                                                 "arguments": "not-an-object"}, timeout=60)
        self.assertIn("error", response)
        self.assertEqual(response["error"]["code"], -32602)

    def test_12_unknown_method(self):
        client = self._client()
        response = client.request("resources/list", {}, timeout=60)
        self.assertIn("error", response)
        self.assertEqual(response["error"]["code"], -32601)

    def test_13_parse_error(self):
        client = self._client()
        client.send_raw(b"{this is not json}\n")
        item = client.wait_any(timeout=10)
        self.assertIsNotNone(item, "未收到 parse error 响应")
        self.assertEqual(item.get("error", {}).get("code"), -32700)
        # server must still work afterwards
        response = client.request("ping")
        self.assertEqual(response.get("result"), {})

    # -- 14 dual-era / legacy negotiation -------------------------------
    def test_14_protocol_era(self):
        client = McpProcess(workdir=WORKDIR, extra_env={"OPENVSP_MCP_FORCE_SUBPROCESS": "0"})
        try:
            response = client.request("initialize", {
                "protocolVersion": "2026-07-28",
                "capabilities": {},
                "clientInfo": {"name": "modern-test-client", "version": "0.0.1"},
            }, timeout=60)
            self.assertIn("result", response)
            self.assertEqual(response["result"].get("protocolVersion"), "2025-11-25")
            meta = response["result"].get("_meta") or {}
            self.assertIn("openvsp-mcp/note", meta)

            response = client.request("server/discover", {}, timeout=60)
            self.assertIn("error", response)
            self.assertEqual(response["error"]["code"], -32601)
            data = response["error"].get("data") or {}
            self.assertEqual(data.get("protocolEra"), "legacy")

            # modern _meta request before a legacy handshake -> clear error
            modern_client = McpProcess(workdir=WORKDIR,
                                       extra_env={"OPENVSP_MCP_FORCE_SUBPROCESS": "0"})
            try:
                modern_client.send_json({
                    "jsonrpc": "2.0", "id": 1, "method": "tools/list",
                    "params": {"_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
                })
                item = modern_client.wait_for_id(1, timeout=60)
                self.assertIn("error", item)
                self.assertEqual(item["error"]["code"], -32600)
            finally:
                modern_client.close()
        finally:
            client.close()

    # -- 15/99 full-suite tests (VSPAERO, report, worker) ----------------
    def test_20_vspaero_polar(self):
        if not FULL[0]:
            self.skipTest("需要 --full（VSPAERO 计算耗时约 10-30 秒）")
        client = self._client()
        result, payload = client.tool_call("openvsp_run_vspaero", {
            "case": "polar",
            "alpha": [0.0, 6.0, 4],
            "mach": 0.05,
            "vinf": 20.0,
            "rho": 1.225,
            "ncpu": 4,
            "wake_iters": 2,
        }, timeout=LONG_TIMEOUT)
        self.assertFalse(result.get("isError"), payload)
        self.assertTrue(payload.get("ok"), payload)
        rows = payload.get("rows") or []
        self.assertGreaterEqual(len(rows), 3, payload.get("warnings"))
        self.assertTrue(any(row.get("CL") for row in rows))
        summary = payload.get("summary") or {}
        self.assertGreater(summary.get("CL_max", 0), 0.2)
        self.assertGreater(summary.get("LD_max", 0), 5)
        files = payload.get("files") or {}
        self.assertTrue(files.get("polar"))
        self.assertTrue(Path(files["polar"]).is_file())
        self.assertEqual(client.raw_stdout, [])

    def test_21_vspaero_stability(self):
        if not FULL[0]:
            self.skipTest("需要 --full（VSPAERO -stab 计算耗时约 10-40 秒）")
        client = self._client()
        result, payload = client.tool_call("openvsp_run_vspaero", {
            "case": "stability",
            "alpha": [2.0, 2.0, 1],
            "mach": 0.05,
            "vinf": 20.0,
            "rho": 1.225,
            "ncpu": 4,
            "wake_iters": 2,
            "mass_kg": 50.0,
            "inertia": {"Iyy": 6.0},
        }, timeout=LONG_TIMEOUT)
        self.assertFalse(result.get("isError"), payload)
        self.assertTrue(payload.get("ok"), payload)
        stability = payload.get("stability") or {}
        self.assertTrue(stability.get("available"), stability)
        derivatives = stability.get("derivatives") or {}
        self.assertIsNotNone(derivatives.get("CL_alpha_per_rad"))
        self.assertIsNotNone(derivatives.get("Cm_alpha_per_rad"))
        modes = stability.get("modes") or {}
        self.assertIn("phugoid", modes)
        files = payload.get("files") or {}
        self.assertTrue(files.get("stab"), payload.get("warnings"))
        self.assertTrue(Path(files["stab"]).is_file())
        self.assertEqual(client.raw_stdout, [])

    def test_22_generate_report(self):
        if not FULL[0]:
            self.skipTest("需要 --full（需要先运行 VSPAERO）")
        client = self._client()
        report_path = WORKDIR / "reports" / "test_report.docx"
        result, payload = client.tool_call(
            "openvsp_generate_report",
            {"report_path": str(report_path),
             "metrics": {"mass_kg": 50.0, "note": "test"}},
            timeout=LONG_TIMEOUT)
        self.assertFalse(result.get("isError"), payload)
        self.assertTrue(payload.get("ok"), payload)
        path = Path(payload["report_path"])
        self.assertTrue(path.is_file(), path)
        self.assertGreater(path.stat().st_size, 2000)
        self.assertIn(payload.get("engine"), ("ai_design", "python-docx", "builtin-docx"))
        # validate the docx is a zip container
        import zipfile
        self.assertTrue(zipfile.is_zipfile(path))

    def test_24_vspaero_beta(self):
        if not FULL[0]:
            self.skipTest("需要 --full（VSPAERO beta 扫描约 10-40 秒）")
        client = self._client()
        result, payload = client.tool_call("openvsp_run_vspaero", {
            "case": "beta",
            "alpha": 2.0,
            "beta": [-2.0, 2.0, 3],
            "mach": 0.05,
            "vinf": 20.0,
            "rho": 1.225,
            "ncpu": 4,
            "wake_iters": 2,
        }, timeout=LONG_TIMEOUT)
        self.assertFalse(result.get("isError"), payload)
        self.assertTrue(payload.get("ok"), payload)
        rows = payload.get("rows") or []
        self.assertGreaterEqual(len(rows), 3, payload.get("warnings"))
        summary = payload.get("summary") or {}
        beta_info = summary.get("beta_sweep") or {}
        self.assertIsNotNone(beta_info.get("CY_beta_per_rad"), summary)
        self.assertIsNotNone(beta_info.get("Cn_beta_per_rad"), summary)
        files = payload.get("files") or {}
        self.assertTrue(files.get("polar"))

    def test_23_worker_subprocess_mode(self):
        if not FULL[0]:
            self.skipTest("需要 --full（worker 模式）")
        client = McpProcess(workdir=WORKDIR,
                            extra_env={"OPENVSP_MCP_FORCE_SUBPROCESS": "1"})
        try:
            response = client.request("initialize", {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "worker-test", "version": "0.1.0"}}, timeout=60)
            self.assertIn("result", response)
            client.notify("notifications/initialized")
            result, status = client.tool_call("openvsp_status", {}, timeout=TOOL_TIMEOUT)
            self.assertFalse(result.get("isError"), status)
            self.assertTrue(status.get("openvsp", {}).get("available"))
            result, built = client.tool_call("openvsp_build_aircraft", {"spec": {
                "name": "worker_uav", "S_ref": 1.0, "AR": 7.0}}, timeout=LONG_TIMEOUT)
            self.assertFalse(result.get("isError"), built)
            self.assertTrue(built.get("ok"), built)
            result, geom = client.tool_call("openvsp_get_geometry", {}, timeout=TOOL_TIMEOUT)
            self.assertFalse(result.get("isError"), geom)
            self.assertGreaterEqual(geom.get("summary", {}).get("geometry_count", 0), 3)
        finally:
            client.close()

    # -- 99 shutdown -----------------------------------------------------
    def test_99_shutdown(self):
        client = self._client()
        response = client.request("shutdown", {}, timeout=60)
        self.assertIn("result", response)
        self.assertIsNone(response["result"])
        deadline = time.time() + 10
        while time.time() < deadline and client.proc.poll() is None:
            time.sleep(0.1)
        self.assertIsNotNone(client.proc.poll(), "shutdown 后服务器进程未退出")
        self.assertEqual(client.proc.returncode, 0, client.stderr_tail())


def main():
    argv = [sys.argv[0]]
    for arg in sys.argv[1:]:
        if arg == "--full":
            FULL[0] = True
        else:
            argv.append(arg)
    if os.environ.get("OPENVSP_MCP_FULL_TESTS") == "1":
        FULL[0] = True
    global TOOL_TIMEOUT
    if FULL[0]:
        TOOL_TIMEOUT = 900.0
    unittest.main(argv=argv, verbosity=2)


if __name__ == "__main__":
    main()
