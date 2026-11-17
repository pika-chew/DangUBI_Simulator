package main

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	mqtt "github.com/eclipse/paho.mqtt.golang"
)

var errPausePending = errors.New("진행 중 응답을 집계한 뒤 일시중지")

type TransmissionSummary struct {
	Mode          string `json:"mode"`
	MaxInFlight   int    `json:"max_in_flight"`
	QueueCapacity int    `json:"queue_capacity"`
	Queued        int    `json:"queued_messages"`
	InFlight      int    `json:"in_flight"`
	PeakQueued    int    `json:"peak_queued"`
	PeakInFlight  int    `json:"peak_in_flight"`
	Backpressure  int    `json:"backpressure_count"`
	Discarded     int    `json:"discarded_queued"`
}

func effectiveInFlight(c Config) int {
	if c.MaxInFlight == 0 {
		return 1
	}
	return c.MaxInFlight
}
func effectiveQueueCapacity(c Config) int {
	if c.QueueCapacity == 0 {
		return 100
	}
	return c.QueueCapacity
}
func validateTransmission(c Config) error {
	if c.PublishMode != "" && c.PublishMode != "serial" && c.PublishMode != "parallel" {
		return errors.New("전송 처리 방식을 확인하세요.")
	}
	if c.MaxInFlight < 0 || c.MaxInFlight > 100 || c.QueueCapacity < 0 || c.QueueCapacity > 1000 {
		return errors.New("동시 처리1~100·대기열1~1000 상한을 확인하세요.")
	}
	if effectiveInFlight(c) > c.ClientCount || c.PublishMode != "parallel" && effectiveInFlight(c) != 1 {
		return errors.New("동시 처리 수는 Client 수 이하이며 순차 방식은1입니다.")
	}
	return nil
}

type queuedPublish struct {
	topic   string
	payload []byte
	offset  int64
}
type publishCompletion struct {
	id      string
	elapsed time.Duration
	err     error
}

// 생성·집계·접속·발행 시작은 한 이벤트 루프에서 처리한다.
// goroutine은 이미 시작된 토큰을 기다리고 결과만 돌려주므로 생성 cache/집계의 동시 접근이 없다.
func runConcurrentMQTT(ctx context.Context, cancel context.CancelFunc, req Request, out *Result, metrics *performanceMetrics, clients map[string]mqtt.Client, remaining map[string]int, scheduled time.Time,
	connect func(string, mqtt.Client) error, report func(*Result, bool), fail func(string), finish func() Result) (Result, error) {
	state := out.Transmission
	queues := map[string][]queuedPublish{}
	busy := map[string]bool{}
	ids := []string{}
	for _, a := range out.Assignments {
		if _, ok := queues[a.ClientID]; !ok {
			ids = append(ids, a.ClientID)
			queues[a.ClientID] = nil
		}
	}
	completed := make(chan publishCompletion, state.MaxInFlight)
	control := controlFrom(ctx)
	seq, index, cursor := 0, 0, 0
	nextPublish := time.Time{}
	step := time.Second / time.Duration(req.MQTT.PublishPerSecond)
	failed := false
	full := false
	var generationError error
	stop := func(message string) {
		if !failed {
			fail(message)
			failed = true
			cancel()
		}
	}
	ack := func(done publishCompletion) {
		state.InFlight--
		busy[done.id] = false
		metrics.ack.add(done.elapsed)
		if done.err != nil {
			out.Unconfirmed++
			stop("발행 응답 미확인으로 실행을 종료합니다. 자동 재발행하지 않습니다.")
		} else {
			out.Published++
			out.Puback++
			remaining[done.id]--
			if req.Config.Lifetime == "while_driving" && remaining[done.id] == 0 {
				clients[done.id].Disconnect(0)
			}
		}
		out.ActiveClients = 0
		for _, client := range clients {
			if client.IsConnectionOpen() {
				out.ActiveClients++
			}
		}
		report(out, true)
	}
	for {
		// 완료된 응답을 먼저 집계하고 같은 Client의 다음 메시지를 허용한다.
		for draining := true; draining; {
			select {
			case done := <-completed:
				ack(done)
			default:
				draining = false
			}
		}
		if ctx.Err() != nil && !failed {
			stop("실행 중단 또는 시험 제한시간 초과입니다.")
		}
		if failed {
			if state.InFlight > 0 {
				ack(<-completed)
				continue
			}
			state.Discarded = state.Queued
			state.Queued = 0
			return finish(), generationError
		}
		// 정지 완료는 모든 진행 중 토큰을 집계한 뒤에만 알린다. 대기열은 고정 보존한다.
		pausing := control.pauseRequested()
		if !pausing || state.InFlight == 0 {
			if err := control.gate(ctx, func(force bool) { report(out, force) }); err != nil {
				stop("실행을 중단했습니다.")
				continue
			}
			pausing = false
		}
		if !pausing {
			// 한 번에 생성량을 제한해 응답 집계·중단 처리를 굶기지 않는다.
			for batch := 0; batch < 32 && seq < req.Config.MessagesPerVehicle && state.Queued < state.QueueCapacity; batch++ {
				if control.pauseRequested() || ctx.Err() != nil {
					break
				}
				due := scheduled.Add(time.Duration(offsetMS(req, seq)+pauseMilliseconds(control)) * time.Millisecond)
				if time.Now().Before(due) {
					break
				}
				started := time.Now()
				a := out.Assignments[index]
				sample := sampleAt(req, index, seq, a, req.MQTT.TopicPrefix)
				if err := recordSample(out, req, sample, index, seq); err != nil {
					generationError = err
					stop("생성 메시지 확인 실패")
					break
				}
				payload, _ := json.Marshal(sample.Message)
				metrics.generation.add(time.Since(started))
				queues[a.ClientID] = append(queues[a.ClientID], queuedPublish{sample.Topic, payload, offsetMS(req, seq)})
				state.Queued++
				state.PeakQueued = max(state.PeakQueued, state.Queued)
				index++
				if index == len(out.Assignments) {
					index = 0
					seq++
				}
			}
			if state.Queued == state.QueueCapacity && !full {
				state.Backpressure++
				full = true
			}
			if state.Queued < state.QueueCapacity {
				full = false
			}
			if !failed && !control.pauseRequested() && ctx.Err() == nil && state.InFlight < state.MaxInFlight && !time.Now().Before(nextPublish) {
				for n := 0; n < len(ids); n++ {
					id := ids[cursor]
					cursor = (cursor + 1) % len(ids)
					if busy[id] || len(queues[id]) == 0 {
						continue
					}
					client := clients[id]
					if !client.IsConnectionOpen() {
						if err := connect(id, client); err != nil {
							if !errors.Is(err, errPausePending) {
								stop("MQTT 연결 또는 재연결 실패")
							}
							break
						}
					}
					if control.pauseRequested() || ctx.Err() != nil {
						break
					}
					job := queues[id][0]
					queues[id][0] = queuedPublish{}
					queues[id] = queues[id][1:]
					state.Queued--
					state.InFlight++
					state.PeakInFlight = max(state.PeakInFlight, state.InFlight)
					busy[id] = true
					delay := max(int64(0), time.Since(scheduled).Milliseconds()-pauseMilliseconds(control)-job.offset)
					out.ScheduleDelayMS = max(out.ScheduleDelayMS, delay)
					metrics.schedule.add(time.Duration(delay) * time.Millisecond)
					out.Attempted++
					report(out, true)
					started := time.Now()
					token := client.Publish(job.topic, 1, false, job.payload)
					nextPublish = time.Now().Add(step)
					go func(id string, token mqtt.Token, started time.Time) {
						err := waitToken(ctx, token, time.Duration(req.MQTT.TimeoutMS)*time.Millisecond)
						completed <- publishCompletion{id, time.Since(started), err}
					}(id, token, started)
					break
				}
			}
		}
		if failed {
			continue
		}
		if seq == req.Config.MessagesPerVehicle && state.Queued == 0 && state.InFlight == 0 {
			out.State = "mqtt_completed"
			return finish(), nil
		}
		report(out, false)
		// 필요할 때만 생산/발행 시각에 깨어나며 대기열이 가득 차면 생성자를 늦춘다.
		delay := 250 * time.Millisecond
		if !pausing && !control.pauseRequested() {
			if seq < req.Config.MessagesPerVehicle && state.Queued < state.QueueCapacity {
				delay = min(delay, max(0, time.Until(scheduled.Add(time.Duration(offsetMS(req, seq)+pauseMilliseconds(control))*time.Millisecond))))
			}
			if state.InFlight < state.MaxInFlight {
				for _, id := range ids {
					if !busy[id] && len(queues[id]) > 0 {
						delay = min(delay, max(0, time.Until(nextPublish)))
						break
					}
				}
			}
		}
		_, _, wake := control.snapshot()
		timer := time.NewTimer(delay)
		select {
		case done := <-completed:
			ack(done)
		case <-ctx.Done():
		case <-wake:
		case <-timer.C:
		}
		timer.Stop()
	}
}
