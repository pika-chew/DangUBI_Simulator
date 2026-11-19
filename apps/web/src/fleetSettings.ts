import type {FleetConfig} from "./FleetControls";

/** 복사한 초안은 현재 시간 배정으로 만들고 원본 기록은 수정하지 않는다. */
export function currentFleet(value:FleetConfig):FleetConfig {
  return {...value,fleet_version:"fleet-plan-4",daily_driving_minutes:undefined,vehicle_variation_percent:undefined,
          trip_min_minutes:value.trip_min_minutes??3,trip_max_minutes:value.trip_max_minutes??120};
}
