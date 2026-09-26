"""Windows AppContainer + Job Object launcher for the sandbox (PLAN.md A9.a.01).

Why: a sandboxed script runs as the user, so plain S0 (scrubbed env) can still
read everything the user can: Windows Credential Manager, Omni's `.env` with
the provider keys, ~/.ssh. Low integrity doesn't stop that (tested
2026-09-26). An AppContainer does: verified live, a Python child inside one is
DENIED the credential vault, ~/.omni/.env, ~/.ssh and writes outside its
granted folder, and has NO network unless the internetClient capability is
granted.

Every process also goes into a Job Object (created suspended, assigned, then
resumed, so nothing escapes the limits): kill-on-close, a memory cap, a
process-count cap. Killing the job kills the whole tree.
"""

from __future__ import annotations

import ctypes
import logging
import msvcrt
import os
import subprocess
import threading
from ctypes import wintypes as wt
from pathlib import Path

log = logging.getLogger(__name__)

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi = ctypes.WinDLL("advapi32", use_last_error=True)
userenv = ctypes.WinDLL("userenv")

HANDLE = ctypes.c_void_p
INFINITE = 0xFFFFFFFF
WAIT_TIMEOUT = 0x102
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 0x1
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
SE_GROUP_ENABLED = 0x4
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x8
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectExtendedLimitInformation = 9
INTERNET_CLIENT_SID = "S-1-15-3-1"
HRESULT_ALREADY_EXISTS = 0x800700B7


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wt.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", wt.BOOL)]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [("AppContainerSid", ctypes.c_void_p), ("Capabilities", ctypes.POINTER(SID_AND_ATTRIBUTES)),
                ("CapabilityCount", wt.DWORD), ("Reserved", wt.DWORD)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
                ("dwX", wt.DWORD), ("dwY", wt.DWORD), ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
                ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
                ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD), ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", HANDLE), ("hStdOutput", HANDLE), ("hStdError", HANDLE)]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE), ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wt.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD),
                ("SchedulingClass", wt.DWORD)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                               "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _sig(fn, restype, *argtypes):
    fn.restype, fn.argtypes = restype, list(argtypes)


_sig(k32.CreatePipe, wt.BOOL, ctypes.POINTER(HANDLE), ctypes.POINTER(HANDLE), ctypes.POINTER(SECURITY_ATTRIBUTES), wt.DWORD)
_sig(k32.SetHandleInformation, wt.BOOL, HANDLE, wt.DWORD, wt.DWORD)
_sig(k32.CreateFileW, HANDLE, wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES), wt.DWORD, wt.DWORD, HANDLE)
_sig(k32.InitializeProcThreadAttributeList, wt.BOOL, ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.c_size_t))
_sig(k32.UpdateProcThreadAttribute, wt.BOOL, ctypes.c_void_p, wt.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
     ctypes.c_void_p, ctypes.c_void_p)
_sig(k32.DeleteProcThreadAttributeList, None, ctypes.c_void_p)
_sig(k32.CreateProcessW, wt.BOOL, wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL, wt.DWORD,
     ctypes.c_void_p, wt.LPCWSTR, ctypes.POINTER(STARTUPINFOEXW), ctypes.POINTER(PROCESS_INFORMATION))
_sig(k32.CreateJobObjectW, HANDLE, ctypes.c_void_p, wt.LPCWSTR)
_sig(k32.SetInformationJobObject, wt.BOOL, HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD)
_sig(k32.AssignProcessToJobObject, wt.BOOL, HANDLE, HANDLE)
_sig(k32.TerminateJobObject, wt.BOOL, HANDLE, wt.UINT)
_sig(k32.ResumeThread, wt.DWORD, HANDLE)
_sig(k32.TerminateProcess, wt.BOOL, HANDLE, wt.UINT)
_sig(k32.WaitForSingleObject, wt.DWORD, HANDLE, wt.DWORD)
_sig(k32.GetExitCodeProcess, wt.BOOL, HANDLE, ctypes.POINTER(wt.DWORD))
_sig(k32.CloseHandle, wt.BOOL, HANDLE)
_sig(advapi.ConvertSidToStringSidW, wt.BOOL, ctypes.c_void_p, ctypes.POINTER(wt.LPWSTR))
_sig(advapi.ConvertStringSidToSidW, wt.BOOL, wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))
_sig(userenv.CreateAppContainerProfile, ctypes.c_long, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_void_p, wt.DWORD,
     ctypes.POINTER(ctypes.c_void_p))
_sig(userenv.DeriveAppContainerSidFromAppContainerName, ctypes.c_long, wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))


class SandboxUnavailable(RuntimeError):
    pass


def _err(what: str) -> SandboxUnavailable:
    return SandboxUnavailable(f"{what} failed (Windows error {ctypes.get_last_error()})")


class AppContainer:
    """One named AppContainer profile. Folders must be granted before a process can use them."""

    def __init__(self, name: str = "OmniBots.Sandbox"):
        self.name = name
        self.sid = ctypes.c_void_p()
        hr = userenv.CreateAppContainerProfile(name, name, "OmniBots bot sandbox", None, 0, ctypes.byref(self.sid))
        if (hr & 0xFFFFFFFF) == HRESULT_ALREADY_EXISTS:
            hr = userenv.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(self.sid))
        if hr != 0:
            raise SandboxUnavailable(f"AppContainer profile: HRESULT {hr & 0xFFFFFFFF:#x}")
        s = wt.LPWSTR()
        if not advapi.ConvertSidToStringSidW(self.sid, ctypes.byref(s)):
            raise _err("ConvertSidToStringSid")
        self.sid_string = s.value
        self._net_sid = ctypes.c_void_p()
        if not advapi.ConvertStringSidToSidW(INTERNET_CLIENT_SID, ctypes.byref(self._net_sid)):
            raise _err("ConvertStringSidToSid")
        self._granted: set[str] = set()
        self._lock = threading.Lock()

    def grant(self, path: Path, *, write: bool = True) -> None:
        """Let the container access this folder (inherited by everything inside).
        write=True → read+write (a workspace); write=False → read+execute only (a library dir)."""
        perm = "(OI)(CI)M" if write else "(OI)(CI)RX"
        key = (str(Path(path).resolve()).lower(), write)
        with self._lock:
            if key in self._granted:
                return
            if write:
                Path(path).mkdir(parents=True, exist_ok=True)
            elif not Path(path).exists():
                return
            r = subprocess.run(["icacls", str(path), "/grant", f"*{self.sid_string}:{perm}", "/Q"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                if not write:              # a read grant we can't set (e.g. a system dir) is not fatal
                    log.debug("skipped read grant on %s: %s", path, (r.stderr or r.stdout).strip())
                    return
                raise SandboxUnavailable(f"could not grant the sandbox access to {path}: {r.stderr.strip() or r.stdout.strip()}")
            self._granted.add(key)

    def spawn(self, cmdline: str, *, cwd: Path, env: dict[str, str], network: bool = False,
              memory_mb: int = 1024, max_processes: int = 32) -> "ContainedProcess":
        sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
        rd, wr = HANDLE(), HANDLE()
        if not k32.CreatePipe(ctypes.byref(rd), ctypes.byref(wr), ctypes.byref(sa), 0):
            raise _err("CreatePipe")
        k32.SetHandleInformation(rd, HANDLE_FLAG_INHERIT, 0)            # only the child's end is inherited
        nul = k32.CreateFileW("NUL", 0x80000000, 0x3, ctypes.byref(sa), 3, 0, None)   # GENERIC_READ, share r/w, OPEN_EXISTING
        job = k32.CreateJobObjectW(None, None)
        if not job:
            raise _err("CreateJobObject")
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_JOB_MEMORY
                                                 | JOB_OBJECT_LIMIT_ACTIVE_PROCESS | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION)
        info.BasicLimitInformation.ActiveProcessLimit = max_processes
        info.JobMemoryLimit = memory_mb * 1024 * 1024
        if not k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)):
            raise _err("SetInformationJobObject")

        caps = (SID_AND_ATTRIBUTES * 1)(SID_AND_ATTRIBUTES(self._net_sid, SE_GROUP_ENABLED))
        sc = SECURITY_CAPABILITIES(self.sid, caps if network else None, 1 if network else 0, 0)
        handles = (HANDLE * 2)(wr, nul)
        size = ctypes.c_size_t()
        k32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        attrs = ctypes.create_string_buffer(size.value)
        if not k32.InitializeProcThreadAttributeList(attrs, 2, 0, ctypes.byref(size)):
            raise _err("InitializeProcThreadAttributeList")
        try:
            if not k32.UpdateProcThreadAttribute(attrs, 0, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, ctypes.byref(sc),
                                                 ctypes.sizeof(sc), None, None):
                raise _err("UpdateProcThreadAttribute(capabilities)")
            if not k32.UpdateProcThreadAttribute(attrs, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles,
                                                 ctypes.sizeof(handles), None, None):
                raise _err("UpdateProcThreadAttribute(handles)")
            si = STARTUPINFOEXW()
            si.StartupInfo.cb = ctypes.sizeof(si)
            si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
            si.StartupInfo.hStdInput, si.StartupInfo.hStdOutput, si.StartupInfo.hStdError = nul, wr, wr
            si.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
            block = None if env is None else ctypes.create_unicode_buffer("".join(f"{k}={v}\0" for k, v in sorted(env.items(), key=lambda kv: kv[0].upper())) + "\0")
            pi = PROCESS_INFORMATION()
            ok = k32.CreateProcessW(None, ctypes.create_unicode_buffer(cmdline), None, None, True,
                                    EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
                                    None if block is None else ctypes.cast(block, ctypes.c_void_p), str(cwd), ctypes.byref(si), ctypes.byref(pi))
            if not ok:
                raise _err("CreateProcess")
        finally:
            k32.DeleteProcThreadAttributeList(attrs)
            k32.CloseHandle(wr)                 # the child holds its own copy; EOF when the whole tree is gone
            k32.CloseHandle(nul)
        if not k32.AssignProcessToJobObject(job, pi.hProcess):
            err = _err("AssignProcessToJobObject")
            k32.TerminateProcess(pi.hProcess, 1)          # still suspended: it never ran
            raise err
        k32.ResumeThread(pi.hThread)
        k32.CloseHandle(pi.hThread)
        return ContainedProcess(pi.hProcess, pi.dwProcessId, job, rd.value)


class ContainedProcess:
    def __init__(self, hprocess, pid: int, job, stdout_handle):
        self.hprocess, self.pid, self.job = hprocess, pid, job
        fd = msvcrt.open_osfhandle(stdout_handle, os.O_RDONLY)
        self.stdout = os.fdopen(fd, "rb", buffering=0)
        self._closed = False

    def wait(self, timeout: float | None = None) -> int | None:
        r = k32.WaitForSingleObject(self.hprocess, INFINITE if timeout is None else int(timeout * 1000))
        if r == WAIT_TIMEOUT:
            return None
        code = wt.DWORD()
        k32.GetExitCodeProcess(self.hprocess, ctypes.byref(code))
        return code.value

    def kill(self) -> None:
        """Kill the whole process tree (everything in the job)."""
        if not self._closed:
            k32.TerminateJobObject(self.job, 1)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        k32.TerminateJobObject(self.job, 1)     # nothing may outlive the run
        k32.CloseHandle(self.job)
        k32.CloseHandle(self.hprocess)
        try:
            self.stdout.close()
        except OSError:
            pass


_instance: AppContainer | None = None
_failed: str | None = None


def container() -> AppContainer | None:
    """The shared OmniBots AppContainer, or None (with the reason logged once) when unavailable."""
    global _instance, _failed
    if _instance is None and _failed is None:
        try:
            _instance = AppContainer()
        except (SandboxUnavailable, OSError) as exc:
            _failed = str(exc)
            log.warning("AppContainer sandbox unavailable, using plain S0: %s", exc)
    return _instance
