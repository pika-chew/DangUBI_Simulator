import type { FleetConfig } from "./FleetControls";
import type { Schedule, Plan } from "./SchedulePanel";
import type { SavedRun, Result } from "./SimulationTools";
export type View = "settings" | "data" | "transmission" | "storage";
export type PlanInput = {title: string; fleet?: FleetConfig; schedule?: Schedule};
type Base = {key:string; id:string; title:string; state:string; created_at:string; vehicle_count:number; planned_messages:number; transport:string};
export type PlanRecord = Base & {kind:"plan"; request?:PlanInput; result?:Omit<Plan,"trips"|"has_more">};
export type RunRecord = Base & {kind:"run"; request?:SavedRun["request"]; result?:Result};
export type WorkspaceRecord = PlanRecord | RunRecord;
