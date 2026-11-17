"""고정 연결을 유지하는 Go MQTT 세션과 한 건씩 교환한다. 접속 프로필은 기록하지 않는다."""
import json
import queue
import subprocess
import threading
import time


class MQTTFailure(Exception):
    pass


class MQTTBridge:
    def __init__(self, worker, data, job):
        self.job=job
        self.profile=data["mqtt"]
        self.stats={}
        self.cleaned=False
        self.forced=False
        self.responses=queue.Queue(maxsize=2)
        self.process=subprocess.Popen([str(worker)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL,text=True,encoding="utf-8",bufsize=1)
        self.reader=threading.Thread(target=self._read,daemon=True)
        self.reader.start()
        self._write({"operation":"mqtt_stream","run_id":job.id,"mqtt":self.profile,"assignments":data["assignments"]})

    def _read(self):
        try:
            while line:=self.process.stdout.readline(262145):
                if len(line)>262144:
                    break
                self.responses.put(json.loads(line),timeout=1)
        except (ValueError,OSError,queue.Full):
            pass
        finally:
            try:self.responses.put(None,timeout=1)
            except queue.Full:pass

    def _write(self,command):
        try:
            self.process.stdin.write(json.dumps(command,separators=(",",":"))+"\n")
            self.process.stdin.flush()
        except (ValueError,OSError) as error:
            raise MQTTFailure("MQTT 세션과 통신하지 못했습니다.") from error

    def receive(self,closing=False):
        profile=self.profile
        deadline=time.monotonic()+(profile["reconnect_attempts"]+1)*(profile["timeout_ms"]/1000+1/profile["connect_per_second"])+3
        stopped=False
        while time.monotonic()<deadline:
            if self.job.stop_requested and not stopped and not closing:
                self._write({"command":"stop"})
                stopped=True
                deadline=min(deadline,time.monotonic()+2)
            try:reply=self.responses.get(timeout=.05)
            except queue.Empty:continue
            if reply is None:break
            if "stats" in reply:
                self.stats=reply["stats"]
                self.cleaned=self.stats["active_clients"]==0
            if not reply.get("ok"):
                raise MQTTFailure(reply.get("error","MQTT 세션 오류"))
            return self.stats
        raise MQTTFailure("MQTT 세션의 최종 응답을 확인하지 못했습니다.")

    def command(self,command,**fields):
        if command!="close":self.cleaned=False
        self._write(dict(command=command,**fields))
        return self.receive(closing=command=="close")

    def close(self):
        try:
            if self.process.poll() is None:
                try:self.command("close")
                except MQTTFailure:pass
                try:self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.cleaned=False
                    self.forced=True
                    self.process.kill()
                    self.process.wait()
        finally:
            self.process.stdin.close()
            self.reader.join(timeout=2)
            self.process.stdout.close()
