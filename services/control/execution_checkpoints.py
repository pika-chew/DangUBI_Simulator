"""실행 전용 차량 종료 상태. UI 캐시 삭제와 분리하고 실행 종료 때 폐기한다."""
from collections import OrderedDict
import threading
from services.control.checkpoints import valid_state,CheckpointError

class ExecutionCheckpoints:
    def __init__(self):
        self.entries=OrderedDict()
        self.lock=threading.RLock()
    def resume(self,scope,before):
        with self.lock:
            keys=[key for key in self.entries if key[0]==scope and key[1]<before]
            if not keys:return 0,None
            key=max(keys,key=lambda k:k[1]);end,state=self.entries[key];self.entries.move_to_end(key)
            return 0,(key[1],end,dict(state))
    def put(self,scope,seq,end,state,epoch):
        if epoch!=0 or not valid_state(state):raise CheckpointError("실행 중간 상태가 유효하지 않습니다.")
        with self.lock:
            self.entries[(scope,seq)]=(end,dict(state));self.entries.move_to_end((scope,seq))
            while len(self.entries)>256:self.entries.popitem(last=False)
    def stats(self):
        with self.lock:return {"entries":len(self.entries),"max_entries":256,"epoch":0}
