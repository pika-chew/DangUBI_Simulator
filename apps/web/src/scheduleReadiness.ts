import type {PlanRecord} from "./workspaceTypes";

type Limits={max_duration_ms:number;max_messages:number;default_clients:number};
export type ExecutionHealth={mqtt_ready:boolean;mqtt_limits:null|{
  execution_profile:string;max_clients:number;max_messages:number;max_duration_seconds:number;
  connect_per_second:number;publish_per_second:number;max_connecting:number;
};execution_profiles:Record<string,Limits>};

export function scheduleReadiness(plan:PlanRecord,transport:"dry_run"|"mqtt",clients:number,health:ExecutionHealth|null) {
  const mode=plan.request?.fleet?.execution_profile==="operations"?"operations":"small_test";
  const limits=health?.execution_profiles?.[mode];
  const measurement=(plan.request?.fleet ?? plan.request?.schedule)?.measurement;
  const start=plan.result?.measurement_start_ms ?? Date.parse(measurement?.start ?? "");
  const end=plan.result?.measurement_end_ms ?? Date.parse(measurement?.end ?? "");
  const duration=Number.isFinite(end-start)?end-start:null;
  const issues:{reason:string;action:string}[]=[];
  if(!Number.isInteger(clients)||clients<1||clients>plan.vehicle_count)issues.push({reason:"Client 수는 차량 수 이하의 양의 정수여야 합니다.",action:"연결 풀 Client 수를 1개부터 차량 수 사이로 지정하세요."});
  const changeProfile=plan.request?.fleet
    ?"설정 복사해 수정 → 실행 프로필을 운영으로 변경 → 일정 생성·기록 → 새 일정을 선택하세요."
    :"수동 일정은 소량 실행만 지원합니다. 기간·메시지를 줄이거나 새 전체 조건에서 운영 프로필로 일정을 생성하세요.";
  if(duration===null)issues.push({reason:"측정 기간을 확인할 수 없습니다.",action:"일정을 다시 선택하거나 조건을 확인해 다시 기록하세요."});
  if(limits&&duration!==null&&duration>limits.max_duration_ms)issues.push({reason:`일정 기간 ${(duration/3600000).toLocaleString()}시간이 ${mode==="operations"?"운영":"소량"} 실행 상한 ${(limits.max_duration_ms/3600000).toLocaleString()}시간을 초과합니다.`,action:mode==="small_test"?changeProfile:"측정 기간을 줄여 새 일정을 기록하세요."});
  if(limits&&plan.planned_messages>limits.max_messages)issues.push({reason:`예정 ${plan.planned_messages.toLocaleString()}건이 일정 실행 상한 ${limits.max_messages.toLocaleString()}건을 초과합니다.`,action:mode==="small_test"?changeProfile:"차량 수·기간을 줄이거나 Microtrip 주기를 늘려 새 일정을 기록하세요."});
  const server=health?.mqtt_limits;
  const mqtt=server?{clients:Math.min(server.max_clients,mode==="small_test"?20:25000),messages:Math.min(server.max_messages,mode==="small_test"?100:1e12),seconds:Math.min(server.max_duration_seconds,mode==="small_test"?30:2592600)}:null;
  if(transport==="mqtt"){
    if(health&&!health.mqtt_ready)issues.push({reason:"서버 MQTT 접속 설정이 준비되지 않았습니다.",action:"서버의 DANGUBI_MQTT_PROFILE이 가리키는 비공개 파일에 endpoint·인증서·토픽과 활성 설정을 준비하세요. 화면에서는 인증 정보를 입력하지 않습니다."});
    if(mode==="operations"&&server&&server.execution_profile!=="operations")issues.push({reason:"일정은 운영 프로필이지만 서버는 소량 MQTT 프로필입니다.",action:"configs/mqtt.operations.example.json을 비공개 경로에 복사해 실제 접속 정보·상한을 설정하고 서버에 적용한 뒤 설정을 다시 확인하세요."});
    if(mqtt){
      if(clients>mqtt.clients)issues.push({reason:`선택 Client ${clients.toLocaleString()}개가 현재 MQTT 상한 ${mqtt.clients.toLocaleString()}개를 초과합니다.`,action:"연결 풀의 Client 수를 줄이거나 운영 일정·서버 프로필의 허용 범위를 함께 확인하세요."});
      if(plan.planned_messages>mqtt.messages)issues.push({reason:`예정 ${plan.planned_messages.toLocaleString()}건이 현재 MQTT 상한 ${mqtt.messages.toLocaleString()}건을 초과합니다.`,action:mode==="small_test"?changeProfile:"서버 MQTT max_messages를 확인하고, 시험 범위를 줄이거나 허용할 상한을 직접 설정하세요."});
      if(duration!==null&&duration>=mqtt.seconds*1000)issues.push({reason:`일정 기간은 현재 MQTT 제한 ${mqtt.seconds.toLocaleString()}초보다 짧아야 합니다.`,action:mode==="small_test"?changeProfile:"접속·응답·정리 여유를 포함하도록 서버 max_duration_seconds를 설정하거나 측정 기간을 줄이세요."});
    }
  }
  return {mode,limits,duration,mqtt,issues};
}
