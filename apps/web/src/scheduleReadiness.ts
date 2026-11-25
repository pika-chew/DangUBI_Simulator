import type {PlanRecord} from "./workspaceTypes";

type Limits={max_duration_ms:number;max_messages:number;default_clients:number};
export type ExecutionHealth={mqtt_ready:boolean;mqtt_limits:null|{
  execution_profile:string;max_clients:number;max_messages:number;max_duration_seconds:number;
  connect_per_second:number;publish_per_second:number;max_connecting:number;
};execution_profiles:Record<string,Limits>};

export function scheduleReadiness(plan:PlanRecord,transport:"dry_run"|"mqtt",clients:number,health:ExecutionHealth|null,realtime=true) {
  const mode=plan.request?.fleet?.execution_profile==="operations"?"operations":"small_test";
  const limits=health?.execution_profiles?.[mode];
  const measurement=(plan.request?.fleet ?? plan.request?.schedule)?.measurement;
  const start=plan.result?.measurement_start_ms ?? Date.parse(measurement?.start ?? "");
  const end=plan.result?.measurement_end_ms ?? Date.parse(measurement?.end ?? "");
  const duration=Number.isFinite(end-start)?end-start:null;
  const issues:{reason:string;action:string}[]=[];
  if(!Number.isInteger(clients)||clients<1||clients>plan.vehicle_count)issues.push({reason:"Client 수는 차량 수 이하의 양의 정수여야 합니다.",action:"연결 풀 Client 수를 1개부터 차량 수 사이로 지정하세요."});
  const changeProfile=plan.request?.fleet
    ?"설정 복사해 기간·차량 수·주기를 시험 실행 상한 안으로 줄여 새 일정을 기록하세요. 대규모·장기 운영은 상시 운영 탭에서 합니다."
    :"수동 일정은 시험 실행에서만 지원합니다. 기간·메시지를 줄이거나, 대규모·장기 운영은 상시 운영 탭을 사용하세요.";
  if(mode==="operations")issues.push({reason:"이전 운영 프로필 일정입니다. 운영 프로필은 상시 운영으로 이전되어 시험 실행에서 실행하지 않습니다.",action:"상시 운영 탭을 사용하거나, 설정 복사로 시험 일정을 새로 기록하세요."});
  if(duration===null)issues.push({reason:"측정 기간을 확인할 수 없습니다.",action:"일정을 다시 선택하거나 조건을 확인해 다시 기록하세요."});
  if(limits&&duration!==null&&duration>limits.max_duration_ms)issues.push({reason:`일정 기간 ${(duration/3600000).toLocaleString()}시간이 ${mode==="operations"?"운영":"시험"} 실행 상한 ${(limits.max_duration_ms/3600000).toLocaleString()}시간을 초과합니다.`,action:mode==="small_test"?changeProfile:"측정 기간을 줄여 새 일정을 기록하세요."});
  if(limits&&plan.planned_messages>limits.max_messages)issues.push({reason:`예정 ${plan.planned_messages.toLocaleString()}건이 일정 실행 상한 ${limits.max_messages.toLocaleString()}건을 초과합니다.`,action:mode==="small_test"?changeProfile:"차량 수·기간을 줄이거나 Microtrip 주기를 늘려 새 일정을 기록하세요."});
  const server=health?.mqtt_limits;
  const mqtt=server?{clients:server.max_clients,messages:server.max_messages,seconds:server.max_duration_seconds}:null;
  if(transport==="mqtt"){
    if(health&&!health.mqtt_ready)issues.push({reason:"서버 MQTT 접속 설정이 준비되지 않았습니다.",action:"서버의 DANGUBI_MQTT_PROFILE이 가리키는 비공개 파일에 endpoint·인증서·토픽과 활성 설정을 준비하세요. 화면에서는 인증 정보를 입력하지 않습니다."});
    if(mode==="operations"&&server&&server.execution_profile!=="operations")issues.push({reason:"일정은 운영 프로필이지만 서버는 소량 MQTT 프로필입니다.",action:"configs/mqtt.operations.example.json을 비공개 경로에 복사해 실제 접속 정보·상한을 설정하고 서버에 적용한 뒤 설정을 다시 확인하세요."});
    if(mqtt){
      if(clients>mqtt.clients)issues.push({reason:`선택 Client ${clients.toLocaleString()}개가 현재 MQTT 상한 ${mqtt.clients.toLocaleString()}개를 초과합니다.`,action:"연결 풀의 Client 수를 줄이거나 운영 일정·서버 프로필의 허용 범위를 함께 확인하세요."});
      if(plan.planned_messages>mqtt.messages)issues.push({reason:`예정 ${plan.planned_messages.toLocaleString()}건이 현재 MQTT 상한 ${mqtt.messages.toLocaleString()}건을 초과합니다.`,action:mode==="small_test"?changeProfile:"서버 MQTT max_messages를 확인하고, 시험 범위를 줄이거나 허용할 상한을 직접 설정하세요."});
      const minimumSeconds=realtime?(duration===null?null:duration/1000):plan.planned_messages/(server?.publish_per_second??1);
      if(minimumSeconds!==null&&minimumSeconds>=mqtt.seconds)issues.push({
        reason:realtime?`기본 전송의 일정 기간은 서버 실행시간 제한 ${mqtt.seconds.toLocaleString()}초보다 짧아야 합니다.`:`현재 발행 속도에서는 빠른 전송도 서버 실행시간 제한 ${mqtt.seconds.toLocaleString()}초 안에 마칠 수 없습니다.`,
        action:"서버 실행시간 상한과 접속·응답·정리 여유를 확인하거나 시험 범위를 줄이세요."
      });
    }
  }
  return {mode,limits,duration,mqtt,issues};
}
