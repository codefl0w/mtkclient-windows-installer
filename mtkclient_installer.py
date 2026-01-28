from __future__ import annotations

import sys
import os
import ctypes
import shutil
import subprocess
import threading
import time
import winreg
import tempfile
import re
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Tuple, Dict

from PyQt6 import QtCore, QtWidgets, QtGui

VERSION = "V1.0.4"
LOGFILE = Path(os.getenv("TEMP") or tempfile.gettempdir()) / f"mtkclient_installer_{VERSION}.log"
TIMEOUT_WINGET = 60 * 30
TIMEOUT_VS = 60 * 60
TIMEOUT_DOWNLOAD = 60 * 20
PIP_TIMEOUT = 60 * 30
PCT_RE = re.compile(r"(\d{1,3})%")

_LOG_LOCK = threading.Lock()


def timestamped(text: str) -> str:
    return f"[{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]}Z] {text}"


def log_to_file(text: str):
    try:
        with _LOG_LOCK:
            with open(LOGFILE, "a", encoding="utf-8") as f:
                f.write(timestamped(text))
    except Exception:
        pass


def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def relaunch_as_admin():
    python_exe = sys.executable
    params = " ".join([f'"{arg}"' for arg in sys.argv])
    cwd = os.getcwd()
    ctypes.windll.shell32.ShellExecuteW(None, "runas", python_exe, params, cwd, 1)
    sys.exit(0)


def enable_long_paths():
    try:
        key_path = r"SYSTEM\CurrentControlSet\Control\FileSystem"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "LongPathsEnabled", 0, winreg.REG_DWORD, 1)
            log_to_file("LongPathsEnabled set to 1 in Registry.")
            return True
    except Exception as e:
        log_to_file(f"Failed to enable Long Paths: {e}")
        return False


def refresh_environment_path() -> bool:
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        )
        sys_path, _ = winreg.QueryValueEx(key, "PATH")
        sys_path = os.path.expandvars(sys_path)
        winreg.CloseKey(key)
        
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment")
            user_path, _ = winreg.QueryValueEx(key, "PATH")
            user_path = os.path.expandvars(user_path)
            winreg.CloseKey(key)
        except FileNotFoundError:
            user_path = ""
        
        os.environ["PATH"] = f"{sys_path};{user_path}"
        return True
    except Exception as e:
        log_to_file(f"Failed to refresh PATH: {e}\n")
        return False


def _ps_single_quote_escape(s: str) -> str:
    return s.replace("'", "''")

def resource_path(relative: str) -> str:
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative)
    return os.path.join(os.path.abspath("."), relative)


def _choose_download_dest_from_headers(url: str, fallback_name: str, timeout_head: float = 8.0) -> str:
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "mtkclient-installer/1.0"})
        with urllib.request.urlopen(req, timeout=timeout_head) as head:
            cd = head.getheader("Content-Disposition")
            if cd:
                m = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';]+)', cd)
                if m:
                    return m.group(1)
    except Exception:
        pass
    
    parsed = urllib.parse.urlparse(url)
    name = Path(parsed.path).name
    return name or fallback_name


def get_user_runtime_dir(appname: str = "mtkclient") -> Path:
    base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or str(Path.home())
    runtime = Path(base) / appname
    runtime.mkdir(parents=True, exist_ok=True)
    return runtime


def _ps_encode_command(script: str) -> str:
    import base64
    encoded = script.encode('utf-16le')
    return base64.b64encode(encoded).decode('ascii')


def _download_with_retry(download_func, max_attempts: int = 3, base_delay: float = 2.0):
    for attempt in range(max_attempts):
        success, msg = download_func()
        if success:
            return True, msg
        
        if attempt < max_attempts - 1:
            delay = base_delay * (2 ** attempt)
            time.sleep(delay)
    
    return False, msg


def find_python_executable() -> str:
    is_frozen = getattr(sys, 'frozen', False)
    
    py = shutil.which("python")
    if py:
        if not (is_frozen and os.path.samefile(py, sys.executable)):
            return py

    roots = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Python",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python",
        Path(r"C:\Python")
    ]

    found_exes = []
    for root in roots:
        if root.exists():
            for folder in root.iterdir():
                if folder.is_dir() and folder.name.lower().startswith("python3"):
                    exe_path = folder / "python.exe"
                    if exe_path.exists():
                        found_exes.append(exe_path)

    if found_exes:
        found_exes.sort(key=lambda x: x.parent.name, reverse=True)
        return str(found_exes[0])

    pylauncher = shutil.which("py")
    if pylauncher:
        try:
            out = subprocess.check_output([pylauncher, "-3", "-c", "import sys;print(sys.executable)"], 
                                          text=True, timeout=5).strip()
            if out and Path(out).exists():
                return out
        except:
            pass

    return sys.executable if not is_frozen else ""


class CommandResult:
    def __init__(self, exitcode: int, stdout: str, stderr: str):
        self.exitcode = exitcode
        self.stdout = stdout
        self.stderr = stderr


class InstallerWorker(QtCore.QThread):
    progress_changed = QtCore.pyqtSignal(int)
    current_progress_changed = QtCore.pyqtSignal(int)
    set_indeterminate = QtCore.pyqtSignal(bool)
    status_changed = QtCore.pyqtSignal(str)
    log_summary = QtCore.pyqtSignal(str)
    log_debug = QtCore.pyqtSignal(str)
    step_failed = QtCore.pyqtSignal(str, int)
    step_succeeded = QtCore.pyqtSignal(str, int)
    finished_all = QtCore.pyqtSignal()
    reboot_required = QtCore.pyqtSignal()

    def __init__(self, start_from_step: int = 0, parent=None):
        super().__init__(parent)
        self._stop_requested = False
        self._start_from_step = start_from_step
        self._needs_reboot = False
        self._current_proc: Optional[subprocess.Popen] = None
        self._reader_threads: List[threading.Thread] = []
        self.steps: List[Tuple[str, callable]] = []
        self._setup_steps()

    def request_stop(self):
        self._stop_requested = True
        self.kill_current_process_tree()

    def kill_current_process_tree(self):
        try:
            proc = self._current_proc
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], 
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
        except Exception:
            pass

    def _setup_steps(self):
        self.steps = [
            ("Ensure winget", self.step_ensure_winget),
            ("Install Git & Python 3.1x", self.step_install_git_python),
            ("Refresh Environment PATH", self.step_refresh_path),
            ("Install WinFsp and UsbDk", self.step_install_winfsp_usbdk),
            ("Install OpenSSL 1.1.1", self.step_install_openssl),
            ("Install Visual Studio Build Tools", self.step_install_build_tools),
            ("Clone mtkclient Repo", self.step_clone_repo),
            ("Install Python Requirements", self.step_pip_install_requirements),
            ("Verify Installation", self.step_verify),
        ]

    def _run_proc(self, cmd: List[str], cwd: Optional[str] = None, 
                  env: Optional[Dict[str, str]] = None, timeout: Optional[float] = None) -> CommandResult:
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, env=env or os.environ.copy(),
                encoding="utf-8", errors="replace",
            )
        except Exception as e:
            self.log_debug.emit(f"[ERROR] start failed: {e}\n")
            return CommandResult(-1, "", str(e))

        self._current_proc = proc
        stdout_lines: List[str] = []
        stderr_lines: List[str] = []
        stop_time = time.time() + timeout if timeout else None

        def reader(pipe, collector, prefix):
            try:
                while True:
                    line = pipe.readline()
                    if line:
                        collector.append(line)
                        self.log_debug.emit(prefix + line)
                        m = PCT_RE.search(line)
                        if m:
                            try:
                                pct = int(m.group(1))
                                if 0 <= pct <= 100:
                                    self.current_progress_changed.emit(pct)
                            except Exception:
                                pass
                    else:
                        if proc.poll() is not None or self._stop_requested:
                            break
                        if stop_time and time.time() > stop_time:
                            break
                        time.sleep(0.05)
            except Exception:
                pass

        t_out = threading.Thread(target=reader, args=(proc.stdout, stdout_lines, ""))
        t_err = threading.Thread(target=reader, args=(proc.stderr, stderr_lines, "[ERR] "))
        t_out.daemon = True
        t_err.daemon = True
        t_out.start()
        t_err.start()
        self._reader_threads = [t_out, t_err]

        try:
            while proc.poll() is None:
                if self._stop_requested:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], 
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except Exception:
                        pass
                    break
                if stop_time and time.time() > stop_time:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], 
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except Exception:
                        pass
                    break
                time.sleep(0.1)
        except Exception:
            pass

        try:
            rc = proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                rc = proc.wait(timeout=1)
            except Exception:
                rc = -1

        try:
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
        except Exception:
            pass

        for t in self._reader_threads:
            if t.is_alive():
                t.join(timeout=3)

        self._reader_threads = []
        self._current_proc = None
        return CommandResult(rc, "".join(stdout_lines), "".join(stderr_lines))

    def _download_with_progress(self, url: str, dest: Path, timeout: Optional[float] = None) -> Tuple[bool, str]:
        self.log_debug.emit(f"[DL] Starting download: {url}\n")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "mtkclient-installer/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", None) or getattr(resp, "getcode", lambda: None)()
                if status and status >= 400:
                    body = resp.read(1024).decode(errors="replace")
                    self.log_debug.emit(f"[DL] HTTP error {status}: {body}\n")
                    return False, f"HTTP {status}"
                
                length = resp.getheader("Content-Length")
                if length:
                    total = int(length)
                    self.current_progress_changed.emit(0)
                    self.set_indeterminate.emit(False)
                    downloaded = 0
                    chunk = 64 * 1024
                    with open(dest, "wb") as f:
                        while True:
                            if self._stop_requested:
                                return False, "Cancelled"
                            data = resp.read(chunk)
                            if not data:
                                break
                            f.write(data)
                            downloaded += len(data)
                            pct = int(downloaded * 100 / total) if total > 0 else -1
                            if pct >= 0:
                                self.current_progress_changed.emit(min(100, pct))
                    self.current_progress_changed.emit(100)
                    self.log_debug.emit(f"[DL] Downloaded {downloaded} bytes (expected {total})\n")
                    return True, "Downloaded"
                else:
                    self.set_indeterminate.emit(True)
                    with open(dest, "wb") as f:
                        while True:
                            if self._stop_requested:
                                return False, "Cancelled"
                            data = resp.read(64 * 1024)
                            if not data:
                                break
                            f.write(data)
                    self.set_indeterminate.emit(False)
                    self.current_progress_changed.emit(100)
                    return True, "Downloaded (unknown length)"
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read(1024).decode(errors="replace")
            except Exception:
                pass
            self.log_debug.emit(f"[DL] HTTPError {e.code}: {body}\n")
            return False, f"HTTP {e.code}"
        except Exception as e:
            self.log_debug.emit(f"[DL] download failed: {e}\n")
            return False, str(e)

    def run(self):
        total = len(self.steps)
        for idx, (label, fn) in enumerate(self.steps, start=1):
            if idx - 1 < self._start_from_step:
                continue
            if self._stop_requested:
                self.log_summary.emit("Operation cancelled by user.\n")
                return

            self.progress_changed.emit(int((idx - 1) / total * 100))
            self.current_progress_changed.emit(-1)
            self.set_indeterminate.emit(True)
            self.status_changed.emit(f"[{idx}/{total}] {label}")

            summary_msg = f"\n=== Step {idx}/{total}: {label} ===\n"
            self.log_summary.emit(summary_msg)
            log_to_file(summary_msg)

            ok, msg = fn()

            self.set_indeterminate.emit(False)
            if ok:
                ok_msg = f"[OK] {msg}\n"
                self.log_summary.emit(ok_msg)
                log_to_file(ok_msg)
                self.step_succeeded.emit(label, idx - 1)
                self.progress_changed.emit(int((idx) / total * 100))
            else:
                fail_msg = f"[FAILED] {msg}\n"
                self.log_summary.emit(fail_msg)
                log_to_file(fail_msg)
                self.step_failed.emit(label, idx - 1)
                return

        if self._needs_reboot:
            self.reboot_required.emit()
        self.progress_changed.emit(100)
        self.finished_all.emit()

    def step_ensure_winget(self) -> Tuple[bool, str]:
        if shutil.which("winget"):
            return True, "winget found"
        
        self.log_summary.emit("winget not found. Installing required dependencies...\n")
        temp_dir = Path(os.getenv("TEMP") or tempfile.gettempdir())
        
        self.log_summary.emit("Step 1/3: Installing VCLibs dependency...\n")
        vclibs_url = "https://aka.ms/Microsoft.VCLibs.x64.14.00.Desktop.appx"
        vclibs = temp_dir / "Microsoft.VCLibs.x64.14.00.Desktop.appx"
        
        vclibs_url_q = _ps_single_quote_escape(vclibs_url)
        vclibs_q = _ps_single_quote_escape(str(vclibs))
        
        def attempt_vclibs_download():
            ps_script = f"Invoke-WebRequest -Uri '{vclibs_url_q}' -OutFile '{vclibs_q}' -UseBasicParsing"
            encoded_ps = _ps_encode_command(ps_script)
            res = self._run_proc(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded_ps],
                None, timeout=TIMEOUT_DOWNLOAD
            )
            if res.exitcode == 0 and vclibs.exists():
                return True, "Downloaded"
            return False, f"Exit {res.exitcode}"
        
        success, msg = _download_with_retry(attempt_vclibs_download, max_attempts=3, base_delay=1.5)
        
        if success:
            self.log_summary.emit(f"VCLibs downloaded ({vclibs.stat().st_size / 1024:.0f} KB)\n")
            ps_script = f"Add-AppxPackage -Path '{vclibs_q}' -ErrorAction SilentlyContinue"
            encoded_ps = _ps_encode_command(ps_script)
            res_install = self._run_proc(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded_ps],
                None, timeout=60
            )
            if res_install.exitcode == 0:
                self.log_summary.emit("VCLibs installed successfully\n")
            else:
                self.log_summary.emit("VCLibs may already be installed (continuing)\n")
            try:
                vclibs.unlink()
            except:
                pass
        else:
            self.log_summary.emit(f"VCLibs download failed after retries: {msg} (may already be present, continuing)\n")
        
        self.log_summary.emit("Step 2/3: Installing UI.Xaml dependency...\n")
        xaml_url = "https://github.com/microsoft/microsoft-ui-xaml/releases/download/v2.8.6/Microsoft.UI.Xaml.2.8.x64.appx"
        xaml = temp_dir / "Microsoft.UI.Xaml.2.8.x64.appx"
        
        xaml_url_q = _ps_single_quote_escape(xaml_url)
        xaml_q = _ps_single_quote_escape(str(xaml))
        
        def attempt_xaml_download():
            ps_script = f"Invoke-WebRequest -Uri '{xaml_url_q}' -OutFile '{xaml_q}' -UseBasicParsing"
            encoded_ps = _ps_encode_command(ps_script)
            res = self._run_proc(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded_ps],
                None, timeout=TIMEOUT_DOWNLOAD
            )
            if res.exitcode == 0 and xaml.exists():
                return True, "Downloaded"
            return False, f"Exit {res.exitcode}"
        
        success, msg = _download_with_retry(attempt_xaml_download, max_attempts=3, base_delay=1.5)
        
        if success:
            self.log_summary.emit(f"UI.Xaml downloaded ({xaml.stat().st_size / 1024:.0f} KB)\n")
            ps_script = f"Add-AppxPackage -Path '{xaml_q}' -ErrorAction SilentlyContinue"
            encoded_ps = _ps_encode_command(ps_script)
            res_install = self._run_proc(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded_ps],
                None, timeout=60
            )
            if res_install.exitcode == 0:
                self.log_summary.emit("UI.Xaml installed successfully\n")
            else:
                self.log_summary.emit("UI.Xaml may already be installed (continuing)\n")
            try:
                xaml.unlink()
            except:
                pass
        else:
            self.log_summary.emit(f"UI.Xaml download failed after retries: {msg} (may already be present, continuing)\n")
        
        self.log_summary.emit("Step 3/3: Installing App Installer (winget)...\n")
        msix = temp_dir / "Microsoft.DesktopAppInstaller.msixbundle"
        url = "https://aka.ms/Microsoft.DesktopAppInstaller_8wekyb3d8bbwe.msixbundle"
        
        msix_q = _ps_single_quote_escape(str(msix))
        url_q = _ps_single_quote_escape(url)
        
        self.log_summary.emit("Cleaning up any conflicting installations...\n")
        ps_script = "Get-AppxPackage Microsoft.DesktopAppInstaller | Remove-AppxPackage -ErrorAction SilentlyContinue"
        encoded_ps = _ps_encode_command(ps_script)
        cleanup = self._run_proc(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded_ps],
            None, timeout=30
        )
        
        self.log_summary.emit(f"Downloading from {url} (up to 3 retries)...\n")
        
        def attempt_winget_download():
            ps_script = f"Invoke-WebRequest -Uri '{url_q}' -OutFile '{msix_q}' -UseBasicParsing"
            encoded_ps = _ps_encode_command(ps_script)
            res = self._run_proc(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded_ps],
                None, timeout=TIMEOUT_DOWNLOAD
            )
            if res.exitcode == 0 and msix.exists():
                return True, "Downloaded"
            return False, f"Exit {res.exitcode}"
        
        success, msg = _download_with_retry(attempt_winget_download, max_attempts=3, base_delay=2.0)
        
        if not success:
            self.log_summary.emit(f"Download failed after all retries: {msg}\n")
            return False, f"Failed to download winget installer: {msg}"
        
        if not msix.exists():
            return False, f"Download reported success but file not found at {msix}"
        
        file_size = msix.stat().st_size / (1024 * 1024)
        self.log_summary.emit(f"Downloaded {file_size:.2f} MB\n")
        
        self.log_summary.emit("Installing App Installer package...\n")
        
        for attempt in range(2):
            ps_script = f"Add-AppxPackage -Path '{msix_q}' -ErrorAction Stop"
            encoded_ps = _ps_encode_command(ps_script)
            res2 = self._run_proc(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded_ps],
                None, timeout=TIMEOUT_WINGET
            )
            
            self.log_summary.emit(f"Installation attempt {attempt + 1} exit code: {res2.exitcode}\n")
            
            if res2.exitcode == 0:
                break
            
            if res2.stderr:
                self.log_summary.emit(f"Error output: {res2.stderr[:500]}\n")
                
            if attempt == 0:
                self.log_summary.emit("First attempt failed, retrying after cleanup...\n")
                time.sleep(2)
        
        try:
            os.remove(msix)
        except:
            pass
            
        if res2.exitcode != 0:
            if "sideload" in (res2.stderr or "").lower() or "policy" in (res2.stderr or "").lower():
                return False, "App sideloading disabled. Enable in Settings > Update & Security > For Developers."
            if "0x80073CF3" in (res2.stderr or ""):
                return False, "Package conflict detected. Please manually uninstall 'App Installer' from Settings > Apps, then retry."
            return False, f"Installation failed (exit {res2.exitcode}). See output above for details."

        self.log_summary.emit("Waiting for package registration...\n")
        time.sleep(3)
        
        refresh_environment_path()
        
        for attempt in range(3):
            if shutil.which("winget"):
                self.log_summary.emit(f"winget found in PATH\n")
                return True, "winget installed successfully"
            if attempt < 2:
                self.log_summary.emit(f"Waiting for PATH update (attempt {attempt + 1}/3)...\n")
                time.sleep(2)
                refresh_environment_path()
        
        winget_paths = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "winget.exe",
            Path(os.environ.get("ProgramFiles", "")) / "WindowsApps" / "Microsoft.DesktopAppInstaller_8wekyb3d8bbwe" / "winget.exe"
        ]
        
        for wp in winget_paths:
            if wp.exists():
                self.log_summary.emit(f"winget found at {wp}, adding to PATH...\n")
                try:
                    windows_apps = str(wp.parent)
                    if windows_apps not in os.environ.get("PATH", ""):
                        os.environ["PATH"] = os.environ.get("PATH", "") + ";" + windows_apps
                    if shutil.which("winget"):
                        return True, "winget found and added to PATH"
                except Exception as e:
                    self.log_summary.emit(f"Failed to add to PATH: {e}\n")
        
        return False, "winget installed but not accessible. A system restart may be required."

    def step_install_git_python(self) -> Tuple[bool, str]:
            # Install Git/Python and verify functionality 
            
            def get_ver(cmd_list): # version string helper
                try:
                    res = subprocess.run(cmd_list, capture_output=True, text=True, timeout=5)
                    if res.returncode == 0:
                        return res.stdout.strip()
                except Exception: pass
                return None
    
            git_path = shutil.which("git")
            git_ver = get_ver([git_path, "--version"]) if git_path else None
            
            py_exec = find_python_executable()
            py_ver_raw = get_ver([str(py_exec), "--version"]) if py_exec else None
            # Verify it's not a Windows Store stub
            py_ok = py_ver_raw and "Python 3" in py_ver_raw
    
            if git_ver and py_ok:
                self.log_summary.emit(f"Found existing Git: {git_ver}\n")
                self.log_summary.emit(f"Found active Python: {py_ver_raw}\n")
                return True, "Git and Python already installed"
            
            if not shutil.which("winget"):
                return False, "winget not available"
    
            # Refresh sources
            self.log_summary.emit("Refreshing winget sources...\n")
            self._run_proc(["winget", "source", "update"], None, timeout=120)
    

            if not git_ver:
                self.log_summary.emit("Installing Git...\n")
                res = self._run_proc(["winget", "install", "--id", "Git.Git", "-e", 
                                     "--accept-package-agreements", "--accept-source-agreements"], 
                                     None, timeout=TIMEOUT_WINGET)
                if res.exitcode not in (0, 3010):
                    return False, f"Git install failed (Exit: {res.exitcode})"
    
            # Install Python if missing/stubbed
            if not py_ok:
                candidates = [("Python.Python.3.13", True), ("Python.Python.3.12", True), ("Python.Python.3", False)]
                installed = False
                for py_id, use_exact in candidates:
                    self.log_summary.emit(f"Attempting to install {py_id}...\n")
                    cmd = ["winget", "install", "--id", py_id]
                    if use_exact: cmd.append("-e")
                    cmd.extend(["--scope", "machine", "--accept-package-agreements", "--accept-source-agreements",
                                "--override", '/passive PrependPath=1 Include_test=0'])
                    
                    res = self._run_proc(cmd, None, timeout=TIMEOUT_WINGET)
                    if res.exitcode in (0, 3010):
                        if res.exitcode == 3010: self._needs_reboot = True
                        installed = True; break
                
                if not installed:
                    return False, "Python installation failed."
    
            
            # re-query the system to ensure the PATH was updated 
            self.log_summary.emit("Verifying installations...\n")
            
            # Verify Git again
            new_git_path = shutil.which("git")
            new_git_ver = get_ver([new_git_path, "--version"]) if new_git_path else None
            if new_git_ver:
                self.log_summary.emit(f"Verified Git: {new_git_ver}\n")
            else:
                self.log_summary.emit("[WARNING] Git installed but not found in PATH yet.\n")
    
            # Verify Python again
            new_py_exec = find_python_executable()
            new_py_ver = get_ver([str(new_py_exec), "--version"]) if new_py_exec else None
            if new_py_ver and "Python 3" in new_py_ver:
                self.log_summary.emit(f"Verified Python: {new_py_ver}\n")
            else:
                # If winget succeeded but we can't find it, it's a PATH refresh issue
                return False, "Python installed but system PATH is not refreshed. Please reboot and try again."
    
            return True, "Git and Python setup successful"

    def step_refresh_path(self) -> Tuple[bool, str]:
        self.log_summary.emit("Reloading environment variables...\n")
        if refresh_environment_path():
            git_v = shutil.which("git")
            py_v = shutil.which("python")
            return True, f"PATH refreshed. Git: {git_v}, Python: {py_v}"
        return False, "Failed to refresh PATH."

    def step_install_winfsp_usbdk(self) -> Tuple[bool, str]:
        if not shutil.which("winget"):
            return False, "winget missing"
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        if (Path(pf) / "UsbDk" / "UsbDkController.exe").exists():
            self.log_summary.emit("UsbDk already installed.\n")
        else:
            res = self._run_proc(["winget", "install", "--id", "daynix.UsbDk", "-e", "--accept-package-agreements", "--accept-source-agreements"], None, timeout=TIMEOUT_WINGET)
            if res.exitcode == 3010:
                self._needs_reboot = True
            elif res.exitcode not in [0]:
                self.log_summary.emit(f"Warning: UsbDk install returned {res.exitcode}\n")
        if (Path(pf) / "WinFsp").exists() or (Path(pf) / "WinFsp (x86)").exists():
            self.log_summary.emit("WinFsp already installed.\n")
        else:
            res = self._run_proc(["winget", "install", "--id", "WinFsp.WinFsp", "-e", "--accept-package-agreements", "--accept-source-agreements"], None, timeout=TIMEOUT_WINGET)
            if res.exitcode == 3010:
                self._needs_reboot = True
            elif res.exitcode not in [0]:
                self.log_summary.emit(f"Warning: WinFsp install returned {res.exitcode}\n")
        return True, "WinFsp/UsbDk step completed"

    def step_install_openssl(self) -> Tuple[bool, str]:
        # Install OpenSSL 1.1.1 with multiple fallback methods
        # Check if already installed
        candidates = [
            Path(r"C:\OpenSSL-Win64"),
            Path(r"C:\Program Files\OpenSSL-Win64"),
            Path(os.environ.get("ProgramFiles", "")) / "OpenSSL-Win64",
        ]
        for c in candidates:
            if c.exists():
                return True, f"OpenSSL found at {c}"

        # Method 1: Try winget with known package IDs
        if shutil.which("winget"):
            self.log_summary.emit("Installing OpenSSL via winget...\n")
            for pkg_id in ["ShiningLight.OpenSSL", "ShiningLight.OpenSSL.Light"]:
                res = self._run_proc(
                    ["winget", "install", "--id", pkg_id, "-e", 
                     "--accept-package-agreements", "--accept-source-agreements"],
                    None, timeout=TIMEOUT_WINGET
                )
                if res.exitcode in [0, 3010]:
                    if res.exitcode == 3010:
                        self._needs_reboot = True
                    return True, f"OpenSSL installed via winget ({pkg_id})"
            
            # Method 2: Search winget for OpenSSL packages
            self.log_debug.emit(timestamped("[OPENSSL] Searching winget catalog...\n"))
            search = self._run_proc(["winget", "search", "openssl"], None, timeout=60)
            found_ids = set()
            for line in (search.stdout or "").splitlines():
                for token in line.strip().split()[:3]:
                    if "." in token and "openssl" in token.lower() and len(token) > 4:
                        found_ids.add(token)
            
            for pkg_id in found_ids:
                self.log_summary.emit(f"Trying: {pkg_id}\n")
                res = self._run_proc(
                    ["winget", "install", "--id", pkg_id, "-e",
                     "--accept-package-agreements", "--accept-source-agreements"],
                    None, timeout=TIMEOUT_WINGET
                )
                if res.exitcode in [0, 3010]:
                    if res.exitcode == 3010:
                        self._needs_reboot = True
                    return True, f"OpenSSL installed via winget ({pkg_id})"

        # Method 3: Direct download fallback
        self.log_summary.emit("Attempting direct download...\n")
        urls = [
            "https://slproweb.com/download/Win64OpenSSL-1_1_1u.exe",
            "https://slproweb.com/download/Win64OpenSSL-1_1_1u-x64.exe",
            "https://sourceforge.net/projects/openssl-for-windows/files/latest/download",
        ]
        
        tempdir = Path(os.getenv("TEMP") or tempfile.gettempdir())
        for url in urls:
            # Get proper filename from headers and validate
            filename = _choose_download_dest_from_headers(url, "Win64OpenSSL-installer.exe")
            dest = tempdir / filename
            
            # Retry wrapper for this specific URL
            def attempt_download():
                return self._download_with_progress(url, dest, timeout=TIMEOUT_DOWNLOAD)
            
            self.log_summary.emit(f"Attempting download from {url} (up to 3 retries)...\n")
            success, msg = _download_with_retry(attempt_download, max_attempts=3, base_delay=2.0)
            
            if not success:
                self.log_summary.emit(f"All download attempts failed for {url}: {msg}\n")
                continue
            
            # ensure we actually downloaded an .exe/.msi
            if dest.suffix.lower() not in (".exe", ".msi"):
                self.log_debug.emit(f"[OPENSSL] downloaded file {dest} is not an executable; removing and skipping.\n")
                try:
                    dest.unlink()
                except Exception:
                    pass
                continue
            
            # Try installation with silent flags
            for flags in [["/verysilent", "/sp-", "/norestart"], ["/S"]]:
                res = self._run_proc([str(dest)] + flags, None, timeout=TIMEOUT_DOWNLOAD)
                try:
                    dest.unlink()
                except:
                    pass
                
                if res.exitcode in [0, 3010]:
                    if res.exitcode == 3010:
                        self._needs_reboot = True
                    return True, "OpenSSL installed via direct download"
            
        return False, "All OpenSSL installation methods failed. Please install manually from slproweb.com"

    def step_install_build_tools(self) -> Tuple[bool, str]:
        # check vcvars64.bat presence
        pf_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        vcvars = (
            Path(pf_x86)
            / "Microsoft Visual Studio"
            / "2022"
            / "BuildTools"
            / "VC"
            / "Auxiliary"
            / "Build"
            / "vcvars64.bat"
        )
    
        if vcvars.exists():
            self.log_summary.emit("VS Build Tools (C++ Workload) detected.\n")
            return True, "VS Build Tools already installed"
    
        # Proceed with installation
        self.set_indeterminate.emit(True)
        self.log_summary.emit("Checking disk space and VS Build Tools requirements...\n")
    
        try:
            total, used, free = shutil.disk_usage(os.environ.get("SystemDrive", "C:"))
            if free < 3 * (1024 ** 3):
                return False, (
                    f"Insufficient disk space. Need ~3GB, "
                    f"have {free / (1024 ** 3):.1f} GB free."
                )
        except Exception:
            self.log_summary.emit("Warning: Could not verify disk space.\n")
    
        if not shutil.which("winget"):
            return False, "winget not present"
    
        self.log_summary.emit(
            "Installing Visual Studio Build Tools (Desktop C++ workload).\n"
        )
    
        cmd = [
            "winget",
            "install",
            "--id",
            "Microsoft.VisualStudio.2022.BuildTools",
            "-e",
            "--accept-package-agreements",
            "--accept-source-agreements",
            "--override=--quiet --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools",
        ]
    
        res = self._run_proc(cmd, None, timeout=TIMEOUT_VS)
    
        # Re-verify system state after installer exits
        if vcvars.exists():
            self.log_summary.emit("VS Build Tools detected after installer run.\n")
            return True, "VS Build Tools already installed"
    
        # Reboot-required success
        if res.exitcode == 3010:
            self._needs_reboot = True
            self.log_summary.emit(
                "[NOTICE] VS Build Tools installation requires reboot.\n"
            )
            return True, "VS Build Tools installed (REBOOT REQUIRED)"
    
        # Known Visual Studio no-op / already-installed HRESULT
        if res.exitcode == 2316632107:
            return True, "VS Build Tools already present (no changes needed)"
    
        # Zero exit but no files detected → defer to reboot
        if res.exitcode == 0:
            self._needs_reboot = True
            return True, "VS Build Tools install finished (verify after reboot)"
    
        return False, f"VS Build Tools failed (exit {res.exitcode})"


    def step_clone_repo(self) -> Tuple[bool, str]:
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        target = Path(pf) / "mtkclient"
        
        if not shutil.which("git"):
            return False, "git not found in PATH"
        
        # Enable indeterminate progress for git 
        self.set_indeterminate.emit(True)
        
        # clone with verification and retry
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        target.mkdir(parents=True, exist_ok=True)

        def verify_repo_ok(t: Path) -> bool:
            # Check presence of core files
            return (t / "mtk_gui.py").exists() or (t / "mtkclient" / "mtk_main.py").exists()

        # If .git exists, verify it's valid
        is_valid_repo = False
        if (target / ".git").exists():
            check = self._run_proc(["git", "rev-parse", "--git-dir"], str(target), timeout=10)
            if check.exitcode == 0 and verify_repo_ok(target):
                is_valid_repo = True
            else:
                self.log_summary.emit("Incomplete repository detected, cleaning up .git for fresh clone...\n")
                try:
                    shutil.rmtree(target / ".git", ignore_errors=True)
                except Exception as e:
                    self.log_summary.emit(f"Warning: could not remove .git: {e}\n")
                is_valid_repo = False

        if is_valid_repo:
            self.log_summary.emit("Repository exists and valid; pulling latest changes...\n")
            res = self._run_proc(["git", "pull", "--progress"], str(target), env=env, timeout=300)
            if res.exitcode != 0:
                return False, f"Git pull failed (code {res.exitcode})"
        else:
            # Attempt shallow clone first, then full clone if shallow fails
            self.log_summary.emit(f"Cloning mtkclient repository to {target}...\n")
            self.log_summary.emit("This may take 2-5 minutes depending on connection speed...\n")
            
            for attempt, cmd in enumerate([
                ["git", "clone", "--progress", "--depth", "1", "https://github.com/bkerler/mtkclient.git", str(target)],
                ["git", "clone", "--progress", "https://github.com/bkerler/mtkclient.git", str(target)]
            ], start=1):
                self.log_summary.emit(f"Git clone attempt {attempt}...\n")
                # Ensure target is clean
                try:
                    shutil.rmtree(target, ignore_errors=True)
                    target.mkdir(parents=True, exist_ok=True)
                except Exception:
                    pass
                    
                res = self._run_proc(cmd, None, env=env, timeout=600)
                if res.exitcode == 0 and verify_repo_ok(target):
                    self.log_summary.emit("Repository cloned successfully.\n")
                    break
                else:
                    self.log_debug.emit(f"[GIT] clone attempt {attempt} exit {res.exitcode}\n")
            else:
                return False, f"Git clone failed (last exit code {res.exitcode})"
        
     
        # Configure git safe.directory
        self.log_summary.emit("Configuring git safe.directory...\n")
        path_str = str(target).replace("\\", "/")
        self._run_proc(
            ["git", "config", "--system", "--add", "safe.directory", path_str],
            None, timeout=10
        )
        
        # Grant write permissions to current user and Administrators only
        # MTKClient needs to create logs/ and config files
        self.log_summary.emit("Setting folder permissions for current user...\n")
        try:
            # Get current username
            current_user = os.environ.get("USERNAME", "")
            if not current_user:
                self.log_summary.emit("Warning: Could not determine current user.\n")
            else:
                # Grant Modify to current user, Full Control to Administrators
                res1 = self._run_proc(
                    ["icacls", str(target), "/grant", f"{current_user}:(OI)(CI)M", "/T"],
                    None, timeout=120
                )
                res2 = self._run_proc(
                    ["icacls", str(target), "/grant", "Administrators:(OI)(CI)F", "/T"],
                    None, timeout=120
                )
                
                if res1.exitcode == 0 and res2.exitcode == 0:
                    self.log_summary.emit("Folder permissions configured for current user.\n")
                else:
                    self.log_summary.emit(f"Warning: Permission setup returned codes {res1.exitcode}/{res2.exitcode}.\n")
        except Exception as e:
            self.log_summary.emit(f"Warning: Could not set folder permissions: {e}\n")
        
        self.set_indeterminate.emit(False)
        return True, "Repository cloned and configured with proper permissions"

    def step_pip_install_requirements(self) -> Tuple[bool, str]:
        # Install Python dependencies for MTKClient

        # Guard for empty Python executable
        py_exec = find_python_executable()
        if not py_exec:
            return False, "Python executable not found after install. Please reboot and retry."
        
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        reqs = Path(pf) / "mtkclient" / "requirements.txt"
        
        if not reqs.exists():
            return False, "requirements.txt not found"
        
        # Upgrade pip first
        self.log_summary.emit("Upgrading pip, setuptools, and wheel...\n")
        self._run_proc(
            [py_exec, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"],
            None, timeout=300
        )
        
        # Configure OpenSSL environment for compilation
        env = os.environ.copy()
        for openssl_dir in [r"C:\OpenSSL-Win64", r"C:\Program Files\OpenSSL-Win64"]:
            if Path(openssl_dir).exists():
                env["INCLUDE"] = env.get("INCLUDE", "") + ";" + os.path.join(openssl_dir, "include")
                env["LIB"] = env.get("LIB", "") + ";" + os.path.join(openssl_dir, "lib")
                env["PATH"] = env.get("PATH", "") + ";" + os.path.join(openssl_dir, "bin")
                self.log_summary.emit(f"Using OpenSSL from: {openssl_dir}\n")
                break
        
        # Install requirements
        self.log_summary.emit("Installing Python packages (this may take several minutes)...\n")
        res = self._run_proc(
            [py_exec, "-m", "pip", "install", "-r", str(reqs)],
            str(reqs.parent), env=env, timeout=PIP_TIMEOUT
        )
        
        if res.exitcode == 0:
            return True, "All Python dependencies installed successfully"
        
        return False, f"Package installation failed (exit code {res.exitcode})"

    def step_verify(self) -> Tuple[bool, str]:
        # Verify all critical components are installed and accessible
        failures = []
        
        # Find Python executable
        py_exec = find_python_executable()
        if not py_exec or not Path(py_exec).exists():
            failures.append("Python executable not found")
        else:
            # Verify Python actually works
            try:
                res = self._run_proc([py_exec, "--version"], None, timeout=5)
                if res.exitcode != 0:
                    failures.append("Python executable not functional")
            except Exception:
                failures.append("Python executable not functional")
        
        if not shutil.which("git"):
            failures.append("Git not in PATH")
        
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        mtkclient_path = Path(pf) / "mtkclient"
        
        if not mtkclient_path.exists():
            failures.append("MTKClient directory missing")
        else:
            # Verify key files exist
            required_files = ["mtk_gui.py", "requirements.txt", "mtkclient/__init__.py"]
            for file in required_files:
                if not (mtkclient_path / file).exists():
                    failures.append(f"{file} missing")
        
        if failures:
            return False, "Verification failed: " + ", ".join(failures)
        
        return True, "All components verified successfully."


# ----------------------------- GUI -----------------------------
class StepListItem(QtWidgets.QWidget):
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self._icon = QtWidgets.QLabel()
        self._label = QtWidgets.QLabel(text)
        self._status = QtWidgets.QLabel("")
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.addWidget(self._icon)
        layout.addWidget(self._label)
        layout.addStretch()
        layout.addWidget(self._status)

    def set_state(self, state: str):
        pix = QtGui.QPixmap(20, 20)
        pix.fill(QtCore.Qt.GlobalColor.transparent)
        p = QtGui.QPainter(pix)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        
        if state == "pending":
            p.setBrush(QtGui.QBrush(QtGui.QColor("#ffeb3b")))  # Yellow fill
            p.setPen(QtCore.Qt.GlobalColor.black)
            p.drawEllipse(1, 1, 18, 18)
            self._status.setText("Pending")
            self._status.setStyleSheet("color: #ffeb3b;")  # Yellow text
        elif state == "running":
            p.setBrush(QtGui.QBrush(QtGui.QColor("#2196f3")))  # Blue
            p.setPen(QtCore.Qt.GlobalColor.white)
            p.drawEllipse(1, 1, 18, 18)
            self._status.setText("Running")
            self._status.setStyleSheet("color: #2196f3;")  # Blue text
        elif state == "ok":
            p.setBrush(QtGui.QBrush(QtGui.QColor("#4caf50")))  # Green
            p.setPen(QtCore.Qt.GlobalColor.white)
            p.drawEllipse(1, 1, 18, 18)
            p.setPen(QtCore.Qt.GlobalColor.white)
            p.drawText(pix.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "\u2713")
            self._status.setText("Done")
            self._status.setStyleSheet("color: #4caf50;")  # Green text
        elif state == "warning":
            p.setBrush(QtGui.QBrush(QtGui.QColor("#ff9800")))  # Orange
            p.setPen(QtCore.Qt.GlobalColor.black)
            p.drawEllipse(1, 1, 18, 18)
            p.drawText(pix.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "!")
            self._status.setText("Retry")
            self._status.setStyleSheet("color: #ff9800;")  # Orange text
        elif state == "failed":
            p.setBrush(QtGui.QBrush(QtGui.QColor("#f44336")))  # Red
            p.setPen(QtCore.Qt.GlobalColor.white)
            p.drawEllipse(1, 1, 18, 18)
            p.setPen(QtCore.Qt.GlobalColor.white)
            p.drawText(pix.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "X")
            self._status.setText("Failed")
            self._status.setStyleSheet("color: #f44336;")  # Red text
        
        p.end()
        self._icon.setPixmap(pix)


class InstallerWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MTKClient Windows Auto-Installer")
        self.resize(980, 760)
        icon_path = resource_path("res/icon.ico")
        self.setWindowIcon(QtGui.QIcon(icon_path))
        self.worker: Optional[InstallerWorker] = None
        self._failed_step_index: int = -1
        self._build_ui()
        try:
            with open(LOGFILE, "w", encoding="utf-8") as f:
                f.write(f"=== MTKClient Installer {VERSION} Log ===\n")
        except Exception:
            pass
        if not is_admin():
            self._admin_lbl.setText("<b style='color:red'>RESTARTING AS ADMIN...</b>")
            QtCore.QTimer.singleShot(100, relaunch_as_admin)
        else:
            self._admin_lbl.setText("<b style='color:green'>ADMINISTRATOR ACCESS GRANTED</b>")

    def _build_ui(self):
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        layout.setSpacing(8)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("<h2>MTKClient Installer by fl0w</h2>")
        self._admin_lbl = QtWidgets.QLabel("Checking permissions...")
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self._admin_lbl)
        layout.addLayout(header)

        self.overall_pbar = QtWidgets.QProgressBar()
        self.overall_pbar.setRange(0, 100)
        self.overall_pbar.setFormat("Overall: %p%")
        
        self.current_pbar = QtWidgets.QProgressBar()
        self.current_pbar.setRange(0, 100)
        self.current_pbar.setFormat("Current: %p%")
        
        self.status_lbl = QtWidgets.QLabel("Ready")
        layout.addWidget(self.status_lbl)
        layout.addWidget(self.overall_pbar)
        layout.addWidget(self.current_pbar)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        checklist_widget = QtWidgets.QWidget()
        checklist_layout = QtWidgets.QVBoxLayout(checklist_widget)
        checklist_layout.setContentsMargins(4, 4, 4, 4)
        checklist_layout.setSpacing(4)

        self.step_items: List[StepListItem] = []
        dummy = InstallerWorker()
        for label, _ in dummy.steps:
            item = StepListItem(label)
            item.set_state("pending")
            checklist_layout.addWidget(item)
            self.step_items.append(item)
        checklist_layout.addStretch()
        splitter.addWidget(checklist_widget)

        logs_widget = QtWidgets.QWidget()
        logs_layout = QtWidgets.QVBoxLayout(logs_widget)
        self.tab_logs = QtWidgets.QTabWidget()
        self.summary_log = QtWidgets.QTextEdit()
        self.summary_log.setReadOnly(True)
        self.summary_log.setFont(QtGui.QFont("Consolas", 10))
        self.summary_log.setStyleSheet("background-color: #1e1e1e; color: #eaeaea;")
        self.debug_log = QtWidgets.QTextEdit()
        self.debug_log.setReadOnly(True)
        self.debug_log.setFont(QtGui.QFont("Consolas", 9))
        self.debug_log.setStyleSheet("background-color: #111; color: #cfcfcf;")
        self.tab_logs.addTab(self.summary_log, "Log")
        self.tab_logs.addTab(self.debug_log, "Debug")
        self.tab_logs.setTabEnabled(1, True)

        logs_layout.addWidget(self.tab_logs)
        splitter.addWidget(logs_widget)

        layout.addWidget(splitter, stretch=1)

        # Bottom layout with About link on left and buttons on right
        bottom_layout = QtWidgets.QHBoxLayout()
        
        # About link
        about_link = QtWidgets.QLabel('<a href="#about" style="color: #2196f3;">About</a>')
        about_link.setTextFormat(QtCore.Qt.TextFormat.RichText)
        about_link.setOpenExternalLinks(False)
        about_link.linkActivated.connect(self.show_about)
        about_link.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.PointingHandCursor))
        bottom_layout.addWidget(about_link)
        bottom_layout.addStretch()

        # Buttons
        btn_layout = QtWidgets.QHBoxLayout()
        self.btn_start = QtWidgets.QPushButton("Start Installation")
        self.btn_retry = QtWidgets.QPushButton("Retry Failed Step")
        self.btn_cancel = QtWidgets.QPushButton("Cancel")
        for b in (self.btn_start, self.btn_retry, self.btn_cancel):
            b.setMinimumHeight(40)
            b.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.btn_retry.setEnabled(False)

        btn_layout.addWidget(self.btn_start)
        btn_layout.addWidget(self.btn_retry)
        btn_layout.addWidget(self.btn_cancel)
        
        bottom_layout.addLayout(btn_layout)
        layout.addLayout(bottom_layout)

        self.setCentralWidget(central)

        self.btn_start.clicked.connect(self.on_start)
        self.btn_retry.clicked.connect(self.on_retry)
        self.btn_cancel.clicked.connect(self.on_cancel)

    def show_about(self):
        about_text = f"""
        <h3>MTKClient Windows Installer</h3>
        <p>
            <b>Version:</b> {VERSION} (270120262308)<br>
            <b>Developer:</b>
            <a href="https://github.com/codefl0w">fl0w</a>
        </p>
    
        <p style="margin-top: 15px;">
            <b>Important Notice:</b><br>
            This application is an independent automation and installer tool.
            The developer is <b>not</b> the author of the software being installed.
            All original tools remain the property of their respective authors
            and are distributed under their original licenses.
        </p>
    
        <p style="margin-top: 15px;"><b>Credits & Licenses:</b></p>
        <ul style="margin-top: 5px;">
            <li>
                <b>MTKClient</b> — Created by bkerler<br>
                Licensed under the GNU General Public License V3.0 (GPL-3.0)<br>
                <a href="https://github.com/bkerler/mtkclient">
                    https://github.com/bkerler/mtkclient
                </a>
            </li>
            <li style="margin-top: 8px;">
                <b>Other Components:</b><br>
                Git, Python, OpenSSL, Visual Studio Build Tools, WinFsp, and UsbDk
                are trademarks and products of their respective owners and are
                distributed under their own licenses.
            </li>
        </ul>
    
        <p style="margin-top: 15px;">
            This installer exists solely to automate a complex manual setup process
            on Windows systems and does not modify or replace the original tools.
        </p>
        """
    
        
        msg = QtWidgets.QMessageBox(self)
        msg.setWindowTitle("About")
        msg.setTextFormat(QtCore.Qt.TextFormat.RichText)
        msg.setText(about_text)
        msg.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Ok)
        msg.exec()

    # UI appenders
    def append_summary(self, text: str):
        self.summary_log.moveCursor(QtGui.QTextCursor.MoveOperation.End)
        self.summary_log.insertPlainText(text)
        self.summary_log.ensureCursorVisible()
        log_to_file(text)

    def append_debug(self, text: str):
        self.debug_log.moveCursor(QtGui.QTextCursor.MoveOperation.End)
        self.debug_log.insertPlainText(text)
        self.debug_log.ensureCursorVisible()
        log_to_file(text)

    # control flows
    def on_start(self):
        enable_long_paths()
        self.start_worker(0)

    def on_retry(self):
        if self._failed_step_index >= 0:
            self.start_worker(self._failed_step_index)

    def start_worker(self, step_idx: int):
        # If a worker is running, request stop and poll until it is dead, then start
        if self.worker and self.worker.isRunning():
            try:
                self.worker.request_stop()
                self.worker.kill_current_process_tree()
            except Exception:
                pass
            self.btn_start.setEnabled(False)
            self.btn_retry.setEnabled(False)
            poll = QtCore.QTimer(self)
            poll.setInterval(250)

            def _resume():
                if not self.worker.isRunning():
                    poll.stop()
                    self._start_worker_internal(step_idx)

            poll.timeout.connect(_resume)
            poll.start()
            return
        self._start_worker_internal(step_idx)

    def _start_worker_internal(self, step_idx: int):
        """
        If step_idx == 0 -> fresh run: clear logs and reset all steps
        If step_idx > 0 -> retry: preserve logs and states for steps < step_idx;
                          reset states for step_idx..end to pending and do not clear logs
        """
        if step_idx == 0:
            self.summary_log.clear()
            self.debug_log.clear()
            for it in self.step_items:
                it.set_state("pending")
        else:
            # preserve prior log content and step states up to step_idx-1
            for i, it in enumerate(self.step_items):
                if i < step_idx:
                    continue
                else:
                    it.set_state("pending")

        self.btn_start.setEnabled(False)
        self.btn_retry.setEnabled(False)
        self._failed_step_index = -1
        self.overall_pbar.setValue(int((step_idx) / len(self.step_items) * 100))
        self.current_pbar.setValue(0)

        self.worker = InstallerWorker(start_from_step=step_idx)
        self.worker.progress_changed.connect(self.overall_pbar.setValue)
        self.worker.current_progress_changed.connect(self._on_current_progress)
        self.worker.set_indeterminate.connect(lambda v: self.current_pbar.setRange(0, 0) if v else self.current_pbar.setRange(0, 100))
        self.worker.status_changed.connect(self.status_lbl.setText)
        self.worker.log_summary.connect(self.append_summary)
        self.worker.log_debug.connect(self.append_debug)
        self.worker.step_failed.connect(self.on_fail)
        self.worker.step_succeeded.connect(self.on_success)
        self.worker.finished_all.connect(self.on_done)
        self.worker.reboot_required.connect(self.on_reboot_required)
        self.worker.start()
        # mark the running step visually
        self._mark_running(step_idx)

    def _on_current_progress(self, value: int):
        if value is None or value < 0:
            return
        if self.current_pbar.maximum() == 0:
            self.current_pbar.setRange(0, 100)
        self.current_pbar.setValue(max(0, min(100, value)))

    def _mark_running(self, idx: int):
        for i, it in enumerate(self.step_items):
            if i == idx:
                it.set_state("running")
            else:
                if it._status.text() == "":
                    it.set_state("pending")

    def on_success(self, label: str, idx: int):
        if 0 <= idx < len(self.step_items):
            self.step_items[idx].set_state("ok")

    def on_fail(self, label: str, idx: int):
        self._failed_step_index = idx
        self.btn_start.setEnabled(True)
        self.btn_retry.setEnabled(True)
        self.current_pbar.setRange(0, 100)
        if 0 <= idx < len(self.step_items):
            self.step_items[idx].set_state("failed")
        self.status_lbl.setText(f"Failed at: {label}")

    def on_reboot_required(self):
        QtWidgets.QMessageBox.warning(self, "Reboot Required", "Some components require a system reboot to complete installation. Save work and restart Windows.")

    def on_done(self):
        self.status_lbl.setText("Installation Complete")
        self.btn_start.setEnabled(True)
        self.btn_retry.setEnabled(False)
        self.overall_pbar.setValue(100)
        self.current_pbar.setRange(0, 100)
        self.current_pbar.setValue(100)
        
        # Custom completion dialog with additional options
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        install_path = Path(pf) / "mtkclient"
        
        # Create custom dialog that stays open
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Installation Complete")
        dialog.setMinimumWidth(450)
        
        layout = QtWidgets.QVBoxLayout(dialog)
        
        # Icon and message
        msg_layout = QtWidgets.QHBoxLayout()
        icon_label = QtWidgets.QLabel()
        icon_label.setPixmap(dialog.style().standardPixmap(QtWidgets.QStyle.StandardPixmap.SP_MessageBoxInformation))
        msg_layout.addWidget(icon_label)
        
        text_label = QtWidgets.QLabel(
            f"<b>Installation completed successfully!</b><br><br>"
            f"MTKClient has been installed to:<br><code>{install_path}</code><br><br>"
            f"You can now launch MTKClient GUI or create a desktop shortcut.<br><br>"
            f"<small>Log saved to: {LOGFILE}</small>"
        )
        text_label.setWordWrap(True)
        msg_layout.addWidget(text_label, stretch=1)
        layout.addLayout(msg_layout)
        
        # Buttons
        button_layout = QtWidgets.QHBoxLayout()
        btn_explorer = QtWidgets.QPushButton("Show in Explorer")
        btn_shortcut = QtWidgets.QPushButton("Add Desktop Shortcut")
        btn_ok = QtWidgets.QPushButton("OK")
        btn_ok.setDefault(True)
        
        btn_explorer.clicked.connect(lambda: self._open_in_explorer(install_path))
        btn_shortcut.clicked.connect(lambda: self._create_desktop_shortcut(install_path))
        btn_ok.clicked.connect(dialog.accept)
        
        button_layout.addWidget(btn_explorer)
        button_layout.addWidget(btn_shortcut)
        button_layout.addStretch()
        button_layout.addWidget(btn_ok)
        layout.addLayout(button_layout)
        
        dialog.exec()
        
        self.append_summary(f"\nLog saved to: {LOGFILE}\n")
    
    def _open_in_explorer(self, path: Path):
        try:
            subprocess.run(["explorer", str(path)], shell=False)
            self.append_summary(f"Opened {path} in Explorer\n")
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Error", f"Failed to open Explorer: {e}")
    
    def _create_desktop_shortcut(self, install_path: Path):
            try:
                # 1. Some registry magic to find the desktop directory since OneDrive can break things. Thanks Macroslop
                try:
                    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
                    desktop_raw, _ = winreg.QueryValueEx(key, "Desktop")
                    desktop = Path(os.path.expandvars(desktop_raw))
                    winreg.CloseKey(key)
                except Exception:
                    desktop = Path.home() / "Desktop"
    
                #  Path Setup
                shortcut_path = desktop / "MTKClient.lnk"
                bat_file = install_path / "mtk_gui.bat"
                icon_path = install_path / "mtkclient" / "icon.ico"
                work_dir = get_user_runtime_dir()
    
                #  Use .bat if exists, otherwise fallback to python.exe (.bat must exist, but this should still help if the tool fails somehow)
                if bat_file.exists():
                    target = str(bat_file)
                    args = ""
                else:
                    target = str(find_python_executable())
                    args = f'"{install_path / "mtk_gui.py"}"'
    
                sp = str(shortcut_path).replace("\\", "\\\\")  #
                tp = str(target).replace("\\", "\\\\")         #
                ap = args.replace("\\", "\\\\")                # helpers for ps_cmd below since Python does not allow backlashes inside f"""
                wd = str(work_dir).replace("\\", "\\\\")       #
                ip = str(icon_path).replace("\\", "\\\\")      #
                
                ps_cmd = f"""
                $ws = New-Object -ComObject WScript.Shell;
                $s = $ws.CreateShortcut("{sp}");
                $s.TargetPath = "{tp}";
                $s.Arguments = '{ap}';
                $s.WorkingDirectory = "{wd}";
                $s.IconLocation = "{ip}";
                $s.Save();
                """
                
    
                subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True)
                
                self.append_summary(f"Shortcut created: {shortcut_path}\n")
                QtWidgets.QMessageBox.information(self, "Success", "Desktop shortcut created successfully.")
    
            except Exception as e:
                self.append_debug(f"[SHORTCUT] Failed: {e}\n")
                QtWidgets.QMessageBox.warning(self, "Shortcut Error", f"Failed to create shortcut: {e}")
    def on_cancel(self):
        if self.worker and self.worker.isRunning():
            try:
                self.worker.request_stop()
                self.worker.kill_current_process_tree()
            except Exception:
                pass
            self.append_summary("\nCancellation requested. Cleaning up...\n")
            # re-enable Start after worker fully stops 
            poll = QtCore.QTimer(self)
            poll.setInterval(250)

            def _reenable():
                if not self.worker or not self.worker.isRunning():
                    poll.stop()
                    self.btn_start.setEnabled(True)

            poll.timeout.connect(_reenable)
            poll.start()
        else:
            self.close()

    def _reset_list_visuals(self):
        for it in self.step_items:
            it.set_state("pending")


# ----------------------------- Main -----------------------------
def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    win = InstallerWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
