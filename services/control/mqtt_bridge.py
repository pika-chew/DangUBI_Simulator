"""한 Go 송신 프로세스의 고정 Client들을 명령 ID로 병행 제어한다."""
import json
import queue
import subprocess
import threading
import time
from services.control.message_codec import encode_command


class MQTTFailure(Exception):
    pass


class MQTTBridge:
    def __init__(self, worker, data, job):
        self.job=job
        self.profile=data["mqtt"]
        self.stats={}
        self.cleaned=False
        self.forced=False
        self.closed=False
        self.resources_closed=False
        self.close_acknowledged=False
        self.close_id=None
        self.sequence=0
        self.pending={}
        self.raw_supported=False
        self.limit=len(set(a["client_id"] for a in data["assignments"]))
        self.responses=queue.Queue(maxsize=self.limit+2)
        self.process=subprocess.Popen([str(worker)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL,text=True,encoding="utf-8",bufsize=1)
        self.reader=threading.Thread(target=self._read,daemon=True)
        self.reader.start()
        try:self._write({"operation":"mqtt_stream","run_id":job.id,"mqtt":self.profile,"assignments":data["assignments"]})
        except BaseException:
            self.close(abort=True)
            raise

    def _read(self):
        try:
            while line:=self.process.stdout.readline(262145):
                if len(line.encode("utf-8"))>262144:break
                self.responses.put(json.loads(line),timeout=2)
                line=None
        except (ValueError,OSError,queue.Full):pass
        finally:
            try:self.responses.put(None,timeout=2)
            except queue.Full:pass

    def _write(self,command):
        raw=encode_command(command,raw=self.raw_supported)+"\n"
        cap=8388608 if command.get("operation")=="mqtt_stream" else 262144
        if len(raw.encode())>cap:raise MQTTFailure("MQTT 명령 크기 상한을 초과했습니다.")
        try:
            self.process.stdin.write(raw)
            self.process.stdin.flush()
        except (ValueError,OSError) as error:
            raise MQTTFailure("MQTT 세션과 통신하지 못했습니다.") from error

    def submit(self,command,**fields):
        if self.closed or len(self.pending)>=self.limit:raise MQTTFailure("MQTT 진행 명령 상한을 확인하세요.")
        self.sequence+=1
        identifier=self.sequence
        self._write(dict(id=identifier,command=command,**fields))
        self.pending[identifier]=time.monotonic()
        self.cleaned=False
        return identifier

    def poll(self,wait=0,closing=False):
        try:reply=self.responses.get(timeout=wait)
        except queue.Empty:
            budget=self.profile["max_duration_seconds"]+2
            # 단조 시각 순서로 삽입하고 응답 ID만 제거하므로 첫 항목이 가장 오래됐다.
            if not closing and self.pending and time.monotonic()-next(iter(self.pending.values()))>budget:
                raise MQTTFailure("MQTT 명령 응답 제한시간을 초과했습니다.")
            return None
        if not isinstance(reply,dict):raise MQTTFailure("MQTT 세션이 최종 응답 없이 종료되었습니다.")
        if not isinstance(reply.get("stats"),dict):raise MQTTFailure("MQTT 집계 응답 형식을 확인하세요.")
        # 상세 통계가 없는 완료 응답도 누적 카운터는 즉시 반영한다.
        self.stats.update(reply["stats"])
        identifier=reply.get("id")
        if type(identifier) is not int or identifier<0 or identifier!=0 and identifier not in self.pending:
            raise MQTTFailure("MQTT 응답 ID를 확인하지 못했습니다.")
        self.pending.pop(identifier,None)
        if reply.get("closed"):
            self.closed=True
            self.cleaned=self.stats.get("active_clients")==0
            self.close_acknowledged=identifier==self.close_id and reply.get("ok") is True and not self.pending
        if not reply.get("ok") and not closing:
            raise MQTTFailure(reply.get("error","MQTT 세션 오류"))
        return reply

    def receive(self):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            reply=self.poll(.05)
            if reply is not None:
                if reply.get("id")!=0:raise MQTTFailure("MQTT 초기 응답 ID를 확인하세요.")
                capabilities=reply.get("capabilities",{})
                self.raw_supported=isinstance(capabilities,dict) and capabilities.get("raw_message") is True
                return self.stats
        raise MQTTFailure("MQTT 초기 응답을 확인하지 못했습니다.")

    def close(self,abort=False):
        if self.resources_closed:return
        try:
            if self.process.poll() is None and not self.closed:
                try:
                    if abort:self._write({"command":"stop"})
                    else:
                        self.sequence+=1
                        self.close_id=self.sequence
                        self.pending[self.sequence]=time.monotonic()
                        self._write({"id":self.sequence,"command":"close"})
                    cleanup_seconds=max(2,self.profile["timeout_ms"]/1000+3) if self.profile.get("execution_profile")=="operations" else 2
                    deadline=time.monotonic()+cleanup_seconds
                    while not self.closed and time.monotonic()<deadline:self.poll(.05,closing=True)
                except MQTTFailure:pass
            # 이미 종료했더라도 stdout에 남은 최종 집계를 읽는다.
            while not self.closed:
                try:
                    if self.poll(.05,closing=True) is None:break
                except MQTTFailure:break
            try:self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.cleaned=False;self.forced=True
                self.process.kill();self.process.wait()
        finally:
            try:self.process.stdin.close()
            except (OSError,ValueError):pass
            self.reader.join(timeout=2)
            self.process.stdout.close()
            self.cleaned=self.cleaned and not self.reader.is_alive() and not self.forced
            self.close_acknowledged=self.close_acknowledged and self.process.returncode==0 and self.cleaned
            self.resources_closed=True
