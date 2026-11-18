"""송신 없는 일정 실행 전용 Go 생성 세션. 메시지 전체를 저장하지 않는다."""
import json
import queue
import subprocess
import threading
import time


class GenerationFailure(Exception):
    pass


class GenerationSession:
    def __init__(self, worker, job=None):
        self.job=job
        self.sequence=0
        self.stats={"process_starts":1,"page_requests":0,"peak_cache_estimated_bytes":0,"peak_heap_alloc_bytes":0}
        self.cleaned=False
        self.forced=False
        self.closed=False
        self.close_acknowledged=False
        self.responses=queue.Queue(maxsize=2)
        self.process=subprocess.Popen([str(worker)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding="utf-8",bufsize=1)
        self.reader=threading.Thread(target=self._read,daemon=True)
        self.reader.start()
        try:
            self._write({"operation":"schedule_stream"})
            self._receive(0,5)
        except BaseException:
            self.close(abort=True)
            raise

    def _read(self):
        try:
            while line:=self.process.stdout.readline(1048577):
                if len(line.encode("utf-8"))>1048576: break
                self.responses.put(json.loads(line),timeout=1)
                line=None
        except (ValueError,OSError,queue.Full):
            pass
        finally:
            try:self.responses.put(None,timeout=1)
            except queue.Full:pass

    def _write(self,command):
        raw=json.dumps(command,separators=(",",":"))+"\n"
        if len(raw.encode())>16384:raise GenerationFailure("생성 명령 크기 상한을 초과했습니다.")
        try:
            self.process.stdin.write(raw)
            self.process.stdin.flush()
        except (OSError,ValueError) as error:
            raise GenerationFailure("생성 세션과 통신하지 못했습니다.") from error

    def _receive(self,expected,timeout):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if self.job and self.job.stop_requested:
                self.close(abort=True)
                raise GenerationFailure("일정 생성을 중단했습니다.")
            try:reply=self.responses.get(timeout=.05)
            except queue.Empty:continue
            if not isinstance(reply,dict) or reply.get("id")!=expected or reply.get("ok") is not True:
                raise GenerationFailure("생성 세션의 응답 ID·형식·상태를 확인하지 못했습니다.")
            stats=reply.get("stats",{})
            if not isinstance(stats,dict):raise GenerationFailure("생성 세션 집계 형식을 확인하세요.")
            self.stats.update(stats)
            self.stats["peak_cache_estimated_bytes"]=max(self.stats["peak_cache_estimated_bytes"],stats.get("cache_estimated_bytes",0))
            self.stats["peak_heap_alloc_bytes"]=max(self.stats["peak_heap_alloc_bytes"],stats.get("heap_alloc_bytes",0))
            return reply
        raise GenerationFailure("생성 세션의 응답 제한시간을 초과했습니다.")

    def page(self,payload,timeout=15,byte_limit=1048576):
        if self.closed:raise GenerationFailure("종료된 생성 세션입니다.")
        self.sequence+=1
        self._write({"id":self.sequence,"command":"page","scheduled":payload,"byte_limit":byte_limit})
        reply=self._receive(self.sequence,timeout)
        result=reply.get("scheduled")
        if not isinstance(result,dict) or not isinstance(result.get("messages"),list) or reply.get("next_offset")!=payload["offset"]+len(result["messages"]):
            raise GenerationFailure("생성 페이지의 진행 위치를 확인하지 못했습니다.")
        self.stats["page_requests"]+=1
        return result

    def close(self,abort=False):
        if self.closed:return
        self.closed=True
        try:
            if self.process.poll() is None:
                if abort:
                    try:self._write({"command":"stop"})
                    except GenerationFailure:pass
                    try:self.process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        self.forced=True;self.process.kill()
                else:
                    self.sequence+=1
                    try:
                        self._write({"id":self.sequence,"command":"close"})
                        self._receive(self.sequence,2)
                        self.close_acknowledged=True
                    except (GenerationFailure,OSError):
                        pass
                try:self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.forced=True;self.process.kill();self.process.wait(timeout=2)
            self.cleaned=self.process.poll() is not None
        finally:
            try:self.process.stdin.close()
            except OSError:pass
            self.reader.join(timeout=2)
            try:self.process.stdout.close()
            except OSError:pass
            self.cleaned=self.cleaned and not self.reader.is_alive()
            self.stats["close_acknowledged"]=self.close_acknowledged
