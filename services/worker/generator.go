package main

import (
	"encoding/json"
	"errors"
	"math"
	"math/rand"
)

const generatorVersion = "combustion-demo-1"

// 실차 보정 전의 재현 가능한 개발 모델. 단위는 제품 사용 안내에 명시한다.
type Generation struct {
	ModelVersion          string  `json:"model_version,omitempty"`
	AnomalyVehiclePercent float64 `json:"anomaly_vehicle_percent"`
	AnomalyEventsPerHour  float64 `json:"anomaly_events_per_hour"`
	cache                 *scenarioCache

	Profile          string  `json:"profile"`
	Seed             int64   `json:"seed"`
	StartTimeMS      int64   `json:"start_time_ms"`
	DurationSeconds  int     `json:"duration_seconds"`
	StartLat         float64 `json:"start_lat"`
	StartLon         float64 `json:"start_lon"`
	InitialOdometerM float64 `json:"initial_odometer_m"`
}

type TracePoint struct {
	ElapsedSeconds float64        `json:"elapsed_seconds"`
	State          string         `json:"state"`
	Speed          float64        `json:"speed_kmh"`
	RPM            float64        `json:"rpm"`
	Distance       float64        `json:"distance_m"`
	Context        *MotionContext `json:"context,omitempty"`
}

func normalize(req Request) (Request, error) {
	g := req.Generation
	if g == nil {
		return req, nil
	}
	if len(req.Message) > 0 {
		return req, errors.New("자동 생성 설정과 수동 메시지는 동시에 사용할 수 없습니다.")
	}
	if g.ModelVersion != "" && g.ModelVersion != generatorVersion && g.ModelVersion != scenarioVersion {
		return req, errors.New("생성 모델 버전을 확인하세요.")
	}
	if math.IsNaN(g.AnomalyVehiclePercent) || math.IsNaN(g.AnomalyEventsPerHour) || g.AnomalyVehiclePercent < 0 || g.AnomalyVehiclePercent > 100 || g.AnomalyEventsPerHour < 0 || g.AnomalyEventsPerHour > 60 {
		return req, errors.New("이상 대상 비율·발생 빈도를 확인하세요.")
	}
	clone := *g
	clone.cache = &scenarioCache{plans: map[int]*scenarioPlan{}}
	req.Generation = &clone
	g = req.Generation
	duration := int64(g.DurationSeconds) * 1000
	if (g.Profile != "urban" && g.Profile != "highway" && g.Profile != "congested" && (g.Profile != "mixed" || g.ModelVersion != scenarioVersion)) || g.Seed < -2147483648 || g.Seed > 2147483647 || g.StartTimeMS < 0 || g.StartTimeMS > 4102444800000 || g.DurationSeconds < 10 || g.DurationSeconds > 3600 || req.Config.IntervalMS < 100 || req.Config.IntervalMS > duration/10 || duration%req.Config.IntervalMS != 0 || math.IsNaN(g.StartLat) || math.IsNaN(g.StartLon) || math.IsNaN(g.InitialOdometerM) || math.Abs(g.StartLat) > 85 || math.Abs(g.StartLon) > 180 || g.InitialOdometerM < 0 || g.InitialOdometerM > 1e9 {
		return req, errors.New("자동 생성의 프로필·시각·좌표·운행시간·주기를 확인하세요. 주기는 운행시간을 나누어 떨어지게 하고 최소 10구간을 확보해야 합니다.")
	}
	req.Config.MessagesPerVehicle = int(duration/req.Config.IntervalMS) + 2 // t=0, 종료 Microtrip, Trip 포함
	if req.Config.VehicleCount*req.Config.MessagesPerVehicle > 10000 {
		return req, errors.New("자동 생성은 Trip을 포함해 최대 10,000건입니다.")
	}
	return req, nil
}

func offsetMS(req Request, seq int) int64 {
	offset := int64(seq) * req.Config.IntervalMS
	if req.Generation != nil {
		return min(offset, int64(req.Generation.DurationSeconds)*1000)
	}
	return offset
}

func sampleAt(req Request, index, seq int, a Assignment, prefix string) Sample {
	s := Sample{VehicleID: a.VehicleID, ClientID: a.ClientID, Topic: prefix + "/" + req.RunID + "/" + a.VehicleID + "/obd", Sequence: seq + 1}
	if req.Generation != nil {
		s.Message, s.VehicleState = automaticMessage(req, index, seq)
		if req.Generation.ModelVersion == scenarioVersion {
			_, _, c, events := scenarioAt(req, index, float64(offsetMS(req, seq))/1000)
			s.Context = &c
			s.EventsObserved = events
		}
		return s
	}
	raw, _ := json.Marshal(req.Message)
	json.Unmarshal(raw, &s.Message)
	pld := s.Message["pld"].(map[string]any)
	base := req.Message["pld"].(map[string]any)
	delta := float64(offsetMS(req, seq))
	s.Message["ts"] = req.Message["ts"].(float64) + delta
	pld["tid"] = base["tid"].(float64) + float64(index)
	if s.Message["ty"].(float64) == 4 {
		pld["clt"] = base["clt"].(float64) + delta
	} else {
		for _, key := range []string{"stt", "edt"} {
			if v, ok := base[key].(float64); ok {
				pld[key] = v + delta
			}
		}
	}
	return s
}

var phaseTimes = []float64{0, .1, .3, .5, .6, .7, .8, .9, 1}
var phaseSpeeds = []float64{0, 0, 1, 1, 0, 0, 1, 1, 0}
var phaseNames = []string{"웜업·공회전", "가속", "정속", "감속", "정차·공회전", "가속", "정속", "감속"}

func speedAt(t, duration, peak float64) (float64, string) {
	if t >= duration {
		return 0, "운행 종료·시동 OFF"
	}
	for j := 0; j < len(phaseNames); j++ {
		left, right := phaseTimes[j]*duration, phaseTimes[j+1]*duration
		if t < right {
			return peak * (phaseSpeeds[j] + (phaseSpeeds[j+1]-phaseSpeeds[j])*(t-left)/(right-left)), phaseNames[j]
		}
	}
	return 0, "운행 종료·시동 OFF"
}

// 구간별 선형 속도를 적분한다. 보고 주기를 바꾸어도 거리·연료 집계는 같다.
func totals(t, duration, peak float64) (distance, idle, fuelCut, fuel, coolant float64) {
	for j := 0; j < len(phaseNames); j++ {
		left, right := phaseTimes[j]*duration, phaseTimes[j+1]*duration
		end := min(t, right)
		if end <= left {
			break
		}
		dt := end - left
		v0 := peak * phaseSpeeds[j]
		v1 := peak * (phaseSpeeds[j] + (phaseSpeeds[j+1]-phaseSpeeds[j])*(end-left)/(right-left))
		d := (v0 + v1) / 2 / 3.6 * dt
		distance += d
		if phaseSpeeds[j] == 0 && phaseSpeeds[j+1] == 0 {
			idle += dt
		}
		if phaseSpeeds[j] > phaseSpeeds[j+1] {
			fuelCut += dt
		} else {
			fuel += .8*dt/3600 + d*.00007
		}
	}
	// 냉간 시작 25℃에서 90℃까지 180초 선형 상승의 시간 평균.
	if t > 0 {
		warm := min(t, 180)
		coolant = (25*warm + 65*warm*warm/360 + (t-warm)*90) / t
	}
	return
}

func position(lat, lon, distance float64) (float64, float64) {
	// 시작점에서 동쪽 방위의 대권 경로. 실제 도로 경로는 아니다.
	phi, lambda := lat*math.Pi/180, lon*math.Pi/180
	arc := distance / 6371000
	p := math.Asin(math.Sin(phi) * math.Cos(arc))
	l := lambda + math.Atan2(math.Sin(arc)*math.Cos(phi), math.Cos(arc)-math.Sin(phi)*math.Sin(p))
	return p * 180 / math.Pi, math.Mod(l*180/math.Pi+540, 360) - 180
}

func automaticMessage(req Request, index, seq int) (map[string]any, string) {
	if req.Generation.ModelVersion == scenarioVersion {
		return scenarioMessage(req, index, seq)
	}
	g := req.Generation
	duration := float64(g.DurationSeconds)
	t := float64(offsetMS(req, seq)) / 1000
	target := map[string]float64{"urban": 50, "highway": 95, "congested": 20}[g.Profile]
	variation := .9 + rand.New(rand.NewSource(g.Seed+int64(index)*1000003)).Float64()*.2
	peak := min(target, .72*duration/1.1) * variation // 차량별 편차를 유지하며 가장 짧은 가속 구간도 2m/s² 이하
	speed, state := speedAt(t, duration, peak)
	distance, idle, cut, fuel, coolant := totals(t, duration, peak)
	lat, lon := position(g.StartLat, g.StartLon, distance)
	ts := float64(g.StartTimeMS + offsetMS(req, seq))
	pld := map[string]any{"tid": float64(index + 1)}
	ty := float64(4)
	if seq == req.Config.MessagesPerVehicle-1 {
		ty = 3
		for k, v := range map[string]any{"stt": float64(g.StartTimeMS), "edt": ts, "dis": distance, "tdis": g.InitialOdometerM + distance, "fc": fuel, "stlat": g.StartLat, "stlon": g.StartLon, "edlat": lat, "edlon": lon, "ctp": coolant, "coe": fuel * 2310, "fct": cut, "hsts": peak, "mesp": distance / duration * 3.6, "idt": idle, "btv": 12.6, "gnv": 14.2, "wut": duration * .1, "dtvt": duration - idle} {
			pld[k] = v
		}
		state = "Trip 집계"
	} else {
		rpm, load, throttle, voltage := 800.0, 18.0, 3.0, 12.6 // vv는 시동 OFF 후 배터리 전압을 나타낸다.
		if speed > 0 {
			rpm = 900 + speed*26
			load = 20 + speed/peak*30
			throttle = load * .6
		}
		if state == "가속" {
			load += 25
			throttle += 20
		}
		if state == "감속" {
			load = 8
			throttle = 0
		}
		if t == duration {
			rpm = 0
			load = 0
			throttle = 0
			voltage = 12.6
		}
		for k, v := range map[string]any{"lat": lat, "lon": lon, "clt": ts, "sp": speed, "rpm": math.Round(rpm), "cdit": distance, "fc": fuel, "el": load, "vv": voltage, "tpos": throttle} {
			pld[k] = v
		}
	}
	return map[string]any{"ty": ty, "ts": ts, "pld": pld}, state
}
