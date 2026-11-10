import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { spawnContextProxyDaemon } from "../plugin/lib/proxy-daemon.mjs";

const WIN = process.platform === "win32";
const WAIT_MS = 20_000;
// Fake litellm, shaped like uv's litellm.exe launcher: a detached child has no console, so the
// launcher starts a console grandchild with its own STARTUPINFO, and Windows opens a window for it.
// The grandchild writes whether that window is visible next to the exe, then idles.
const FAKE_CS = `
using System; using System.Diagnostics; using System.IO; using System.Runtime.InteropServices; using System.Threading;
public static class FakeLitellm {
  [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
  struct STARTUPINFO { public int cb; public IntPtr lpReserved, lpDesktop, lpTitle;
    public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
    public short wShowWindow, cbReserved2; public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError; }
  [StructLayout(LayoutKind.Sequential)] struct PROCESS_INFORMATION { public IntPtr hProcess, hThread; public int pid, tid; }
  [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern void GetStartupInfoW(out STARTUPINFO si);
  [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  static extern bool CreateProcessW(string app, string cmd, IntPtr pa, IntPtr ta, bool inherit, uint flags,
    IntPtr env, string cwd, ref STARTUPINFO si, out PROCESS_INFORMATION pi);
  [DllImport("kernel32.dll")] static extern IntPtr GetConsoleWindow();
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
  public static void Main(string[] args) {
    string exe = Process.GetCurrentProcess().MainModule.FileName;
    try { Run(args, exe); } catch (Exception e) { File.WriteAllText(Path.Combine(Path.GetDirectoryName(exe), "console.txt"), "error " + e.GetType().Name + ": " + e.Message); }
  }
  static void Run(string[] args, string exe) {
    if (args.Length > 0 && args[0] == "--child") {
      IntPtr h = GetConsoleWindow();
      string state = h == IntPtr.Zero ? "none" : (IsWindowVisible(h) ? "visible" : "hidden");
      File.WriteAllText(Path.Combine(Path.GetDirectoryName(exe), "console.txt"), state);
      Thread.Sleep(60000);
      return;
    }
    STARTUPINFO si; GetStartupInfoW(out si);
    si.dwFlags &= ~0x100; // STARTF_USESTDHANDLES: the parent's handles are not inherited
    si.cbReserved2 = 0; si.lpReserved2 = IntPtr.Zero; // libuv passes CRT fds here
    PROCESS_INFORMATION pi;
    if (!CreateProcessW(exe, "\\"" + exe + "\\" --child", IntPtr.Zero, IntPtr.Zero, false, 0, IntPtr.Zero, null, ref si, out pi))
      File.WriteAllText(Path.Combine(Path.GetDirectoryName(exe), "console.txt"), "createprocess-failed " + Marshal.GetLastWin32Error());
    Thread.Sleep(60000);
  }
}`;

function buildFake(dir) {
  const src = path.join(dir, "fake.cs");
  fs.writeFileSync(src, FAKE_CS);
  const exe = path.join(dir, "litellm.exe");
  const ps = `Add-Type -TypeDefinition (Get-Content -Raw '${src}') -OutputAssembly '${exe}' -OutputType ConsoleApplication`;
  const r = spawnSync("powershell.exe", ["-NoProfile", "-Command", ps], { encoding: "utf8", windowsHide: true });
  assert.equal(r.status, 0, r.stderr);
  return exe;
}

async function consoleState(dir, pid) {
  const out = path.join(dir, "console.txt");
  const deadline = Date.now() + WAIT_MS;
  try {
    while (!fs.existsSync(out)) {
      if (Date.now() > deadline) throw new Error("fake litellm wrote no console state");
      await new Promise((r) => setTimeout(r, 100));
    }
    return fs.readFileSync(out, "utf8").trim();
  } finally {
    spawnSync("taskkill", ["/T", "/F", "/PID", String(pid)], { windowsHide: true });
    await new Promise((r) => setTimeout(r, 300));
    fs.rmSync(out, { force: true });
  }
}

// Issue #1: the detached proxy opened a console window; closing it killed the proxy.
test("detached proxy spawn gets no visible console window on Windows (#1)", { skip: !WIN && "Windows only" }, async () => {
  const bin = fs.mkdtempSync(path.join(os.tmpdir(), "sc-fake-litellm-"));
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "sc-win-home-"));
  const exe = buildFake(bin);

  // Control: without windowsHide the same spawn must show a window, or this host cannot detect the bug.
  const control = spawn(exe, [], { detached: true, stdio: "ignore" });
  control.unref();
  assert.equal(await consoleState(bin, control.pid), "visible", "control spawn showed no console window; the check is blind here");

  const env = Object.fromEntries(Object.entries(process.env).filter(([k]) => k.toUpperCase() !== "PATH"));
  env.Path = `${bin};${process.env.PATH}`;
  const r = spawnContextProxyDaemon(home, import.meta.url, env);
  assert.equal(r.ok, true, r.reason);
  assert.equal(await consoleState(bin, r.pid), "hidden");
});
