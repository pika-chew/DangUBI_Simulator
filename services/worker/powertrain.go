package main

import (
	"math"
	"math/rand"
	"sort"
)

const powertrainVersion = "combustion-powertrain-5"
const engineTick = .5
const engineCheckpoint = 10.0

// 개발용 가솔린·6단 자동 승용차 근사. 특정 차종/ECU의 실측 보정값은 아니다.
type engineSpec struct {
	Mass, DragArea, Rolling, WheelRadius, FinalDrive               float64
	RatedKW, PeakTorque, IdleRPM, IdleFuel, RestVolts, ChargeVolts float64
}

var forwardRatios = []float64{3.8, 2.25, 1.55, 1.15, .86, .67}

func vehicleEngineSpec(g *Generation, index int) engineSpec {
	rng := rand.New(rand.NewSource(g.Seed + int64(index)*1000003 + 0x706f776572))
	return engineSpec{Mass: 1350 * (.92 + .16*rng.Float64()), DragArea: .62 + .1*rng.Float64(), Rolling: .011 + .003*rng.Float64(), WheelRadius: .30 + .02*rng.Float64(), FinalDrive: 3.9, RatedKW: 120 * (.92 + .16*rng.Float64()), PeakTorque: 230 * (.92 + .16*rng.Float64()), IdleRPM: 750 + 100*rng.Float64(), IdleFuel: .65 + .15*rng.Float64(), RestVolts: 12.5 + .2*rng.Float64(), ChargeVolts: 14.1 + .2*rng.Float64()}
}

type engineState struct {
	Time, Fuel, CutSeconds, TemperatureIntegral, WarmSeconds    float64
	RPM, Coolant, Load, Throttle, FuelRate, WheelPower, Voltage float64
	Gear                                                        int
	LastShift                                                   float64
	FuelCut                                                     bool
}
type enginePlan struct {
	Spec   engineSpec
	States []engineState
}

func motionAtPlan(p *scenarioPlan, t float64) (speed, acceleration, grade float64) {
	i := sort.Search(len(p.Segments), func(i int) bool { return p.Segments[i].End > t })
	if i == len(p.Segments) {
		i--
	}
	s := p.Segments[i]
	a := (s.V1 - s.V0) / (s.End - s.Start)
	return max(0, s.V0+a*max(0, min(t, s.End)-s.Start)), a, s.Grade
}
func roadPower(spec engineSpec, v, a, grade float64) float64 {
	force := spec.Mass*a + spec.Mass*9.80665*(spec.Rolling+grade) + .5*1.225*spec.DragArea*v*v
	return force * v / 1000
}
func engineRPM(spec engineSpec, v float64, gear int) float64 {
	return v / (2 * math.Pi * spec.WheelRadius) * 60 * forwardRatios[gear-1] * spec.FinalDrive
}
func coldFraction(temp float64) float64 { return max(0, min(1, (70-temp)/50)) }
func availableTorque(spec engineSpec, rpm float64) float64 {
	return spec.PeakTorque * (.65 + .35*math.Exp(-math.Pow((rpm-3300)/2600, 2)))
}
func setEnginePoint(p *scenarioPlan, spec engineSpec, s engineState, dt float64) engineState {
	v, a, grade := motionAtPlan(p, s.Time)
	s.WheelPower = roadPower(spec, v, a, grade)
	required := max(0, s.WheelPower/.9) + 1.2
	demand := min(1, required/spec.RatedKW)
	idle := spec.IdleRPM + 250*coldFraction(s.Coolant)
	gear := s.Gear
	if gear < 1 {
		gear = 1
	}
	if v < .5 {
		gear = 1
	} else if s.Time-s.LastShift >= 2 {
		coupled := engineRPM(spec, v, gear)
		if gear > 1 && ((demand > .45 && coupled < 2400) || coupled < 1050+500*demand) {
			gear--
		} else if gear < 6 && coupled > 1800+1700*demand && engineRPM(spec, v, gear+1) > 1150 {
			gear++
		}
	}
	if gear != s.Gear {
		s.LastShift = s.Time
	}
	s.Gear = gear
	// 저속 토크 컨버터 슬립을 제한적으로 근사한다.
	targetRPM := max(idle, engineRPM(spec, v, gear)+max(0, 1-v/10)*demand*500)
	targetRPM = min(6000, targetRPM)
	if dt > 0 {
		s.RPM = targetRPM + (s.RPM-targetRPM)*math.Exp(-dt/.45)
	} else {
		s.RPM = targetRPM
	}
	threshold := 1500.0
	if s.FuelCut {
		threshold = 1200
	}
	s.FuelCut = s.Coolant >= 70 && v > 3 && a <= .05 && s.WheelPower < -1 && s.RPM >= threshold
	if s.FuelCut {
		s.Load = 3
		s.Throttle = 1.5
		s.FuelRate = 0
	} else {
		capacity := max(1, availableTorque(spec, s.RPM)*s.RPM*2*math.Pi/60/1000)
		s.Load = max(8, min(100, 100*required/capacity))
		s.Throttle = max(2, min(95, 4+s.Load*.65+max(0, a)*5))
		if s.WheelPower < 0 {
			s.Throttle = 2
		}
		efficiency := max(.18, min(.32, .20+.12*(1-math.Exp(-required/20))-.02*math.Pow((s.RPM-2800)/4500, 2)))
		s.FuelRate = max(spec.IdleFuel, required/(efficiency*8.8)) * (1 + .20*coldFraction(s.Coolant))
	}
	s.Voltage = spec.RestVolts + (spec.ChargeVolts-spec.RestVolts)*(1-math.Exp(-s.Time/2)) - .04*s.Load/100
	return s
}
func integrateEngine(s engineState, dt float64) engineState {
	out := s
	out.Time += dt
	out.Fuel += s.FuelRate * dt / 3600
	if s.FuelCut {
		out.CutSeconds += dt
	}
	target := 90.0
	if s.FuelCut {
		target = 80
	}
	tau := 600 / (1 + 2*s.Load/100)
	fraction := math.Exp(-dt / tau)
	out.Coolant = target + (s.Coolant-target)*fraction
	out.TemperatureIntegral += target*dt + (s.Coolant-target)*tau*(1-fraction)
	if s.Coolant < 70 {
		warm := dt
		if out.Coolant >= 70 {
			warm = max(0, min(dt, -tau*math.Log((70-target)/(s.Coolant-target))))
		}
		out.WarmSeconds += warm
	}
	return out
}
func advanceEngine(p *scenarioPlan, spec engineSpec, s engineState, dt float64) engineState {
	return setEnginePoint(p, spec, integrateEngine(s, dt), dt)
}
func getEnginePlan(req Request, index int) *enginePlan {
	p := getScenario(req, index)
	if p.Engine != nil {
		return p.Engine
	}
	spec := vehicleEngineSpec(req.Generation, index)
	temp := 20.0
	if req.Generation.EngineStart == "warm" {
		temp = 85
	}
	s := setEnginePoint(p, spec, engineState{Coolant: temp, Gear: 1, LastShift: -2}, 0)
	plan := &enginePlan{Spec: spec, States: []engineState{s}}
	for s.Time < p.Duration-1e-9 {
		s = advanceEngine(p, spec, s, min(engineTick, p.Duration-s.Time))
		if math.Abs(s.Time/engineCheckpoint-math.Round(s.Time/engineCheckpoint)) < 1e-9 || s.Time >= p.Duration {
			plan.States = append(plan.States, s)
		}
	}
	p.Engine = plan
	return plan
}
func engineAt(req Request, index int, t float64) engineState {
	p := getScenario(req, index)
	plan := getEnginePlan(req, index)
	t = max(0, min(p.Duration, t))
	i := sort.Search(len(plan.States), func(i int) bool { return plan.States[i].Time > t }) - 1
	if i < 0 {
		i = 0
	}
	s := plan.States[i]
	for s.Time+engineTick <= t+1e-9 {
		s = advanceEngine(p, plan.Spec, s, engineTick)
	}
	if rest := t - s.Time; rest > 1e-9 {
		// 연료는 고정 격자의 유량으로 적분해 조회 주기가 누적량을 바꾸지 않게 한다.
		next := advanceEngine(p, plan.Spec, s, min(engineTick, p.Duration-s.Time))
		fraction := rest / (next.Time - s.Time)
		partial := integrateEngine(s, rest)
		partial.RPM = s.RPM + (next.RPM-s.RPM)*fraction
		partial.Voltage = s.Voltage + (next.Voltage-s.Voltage)*fraction
		s = partial
	}
	if t >= p.Duration {
		s.RPM = 0
		s.Gear = 0
		s.Load = 0
		s.Throttle = 0
		s.FuelRate = 0
		s.FuelCut = false
		s.Voltage = plan.Spec.RestVolts
	}
	return s
}
func engineStatus(s engineState) string {
	if s.RPM == 0 {
		return "시동 OFF"
	}
	if s.FuelCut {
		return "연료 차단·엔진 브레이크"
	}
	if s.Time-s.LastShift < .75 && s.Time > 0 {
		return "변속·연료 분사"
	}
	if s.Coolant < 70 {
		return "냉간·연료 분사"
	}
	return "온간·연료 분사"
}
func powertrainMessage(req Request, index, seq int) (map[string]any, string) {
	message, state := scenarioMessage(req, index, seq)
	t := float64(offsetMS(req, seq)) / 1000
	s := engineAt(req, index, t)
	spec := getEnginePlan(req, index).Spec
	pld := message["pld"].(map[string]any)
	pld["fc"] = s.Fuel
	if message["ty"].(float64) == 3 {
		pld["coe"] = s.Fuel * 2310
		pld["fct"] = s.CutSeconds
		pld["ctp"] = s.TemperatureIntegral / max(1, t)
		pld["wut"] = s.WarmSeconds
		pld["btv"] = spec.RestVolts
		pld["gnv"] = spec.ChargeVolts
	} else {
		pld["rpm"] = math.Round(s.RPM)
		pld["el"] = s.Load
		pld["tpos"] = s.Throttle
		pld["vv"] = s.Voltage
		if s.FuelCut {
			state += "·연료 차단"
		}
	}
	return message, state
}
func addEngineContext(req Request, index int, t float64, c *MotionContext) {
	s := engineAt(req, index, t)
	c.Gear = s.Gear
	c.EngineState = engineStatus(s)
	c.WheelPower = s.WheelPower
	c.FuelRate = s.FuelRate
	c.Coolant = s.Coolant
}

// 최대 차륜 출력으로 가속 종료 속도를 제한한다. 변속기의 완전한 동역학은 모델링하지 않는다.
func limitPoweredEnd(spec engineSpec, start, end, dt, grade float64) float64 {
	if dt <= 0 || end <= start {
		return end
	}
	budget := (spec.RatedKW - 1.2) * .9
	if roadPower(spec, end, (end-start)/dt, grade) <= budget {
		return end
	}
	lo, hi := start, end
	for i := 0; i < 36; i++ {
		mid := (lo + hi) / 2
		if roadPower(spec, mid, (mid-start)/dt, grade) > budget {
			hi = mid
		} else {
			lo = mid
		}
	}
	return lo
}
