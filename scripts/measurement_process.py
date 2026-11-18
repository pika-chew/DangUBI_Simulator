"""계측용 자식과 하위 Worker를 한 수명으로 관리한다. 제품 서버에는 적용하지 않는다."""
import ctypes
import os
import signal
import subprocess


class ProcessTree:
    def __init__(self):
        self.process=None;self.handle=None
        if os.name!="nt":return
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_=[("process_time",ctypes.c_longlong),("job_time",ctypes.c_longlong),("flags",w.DWORD),
                      ("min_ws",ctypes.c_size_t),("max_ws",ctypes.c_size_t),("processes",w.DWORD),
                      ("affinity",ctypes.c_size_t),("priority",w.DWORD),("scheduling",w.DWORD)]
        class Extended(ctypes.Structure):
            _fields_=[("basic",Basic),("io",ctypes.c_ulonglong*6),("process_memory",ctypes.c_size_t),
                      ("job_memory",ctypes.c_size_t),("peak_process",ctypes.c_size_t),("peak_job",ctypes.c_size_t)]
        self.kernel=ctypes.WinDLL("kernel32",use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR];self.kernel.CreateJobObjectW.restype=w.HANDLE
        self.kernel.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD];self.kernel.SetInformationJobObject.restype=w.BOOL
        self.kernel.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE];self.kernel.AssignProcessToJobObject.restype=w.BOOL
        self.kernel.CloseHandle.argtypes=[w.HANDLE];self.kernel.CloseHandle.restype=w.BOOL
        self.handle=self.kernel.CreateJobObjectW(None,None)
        if not self.handle:raise OSError("계측 프로세스 수명 관리 준비 실패")
        limits=Extended();limits.basic.flags=0x2000 # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle,9,ctypes.byref(limits),ctypes.sizeof(limits)):
            self.close();raise OSError("계측 프로세스 정리 정책 설정 실패")

    def start(self,args,**kwargs):
        if self.process is not None:raise RuntimeError("계측 실행은 하나씩 시작합니다.")
        self.process=subprocess.Popen(args,start_new_session=os.name!="nt",**kwargs)
        if os.name=="nt":
            from ctypes import wintypes
            if not self.kernel.AssignProcessToJobObject(self.handle,wintypes.HANDLE(int(self.process._handle))):
                self.process.kill();self.process.wait();self.close();raise OSError("계측 프로세스 수명 연결 실패")
        return self.process

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle);self.handle=None
        elif os.name!="nt" and self.process:
            try:os.killpg(self.process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
        if self.process and self.process.poll() is None:
            self.process.kill();self.process.wait(timeout=5)
