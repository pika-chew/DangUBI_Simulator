package main

import (
	"math/bits"
	"time"
)

type DurationSummary struct {
	Count      int64   `json:"count"`
	MeanMS     float64 `json:"mean_ms"`
	MaxMS      float64 `json:"max_ms"`
	P95UpperMS float64 `json:"p95_upper_ms"`
	P99UpperMS float64 `json:"p99_upper_ms"`
	Within1s   int64   `json:"within_1s_count"`
	Within2s   int64   `json:"within_2s_count"`
	Over5s     int64   `json:"over_5s_count"`
}
type durationMetric struct {
	count                      int64
	meanNS                     float64
	peak                       time.Duration
	within1s, within2s, over5s int64
	buckets                    [64]int64
}

func (m *durationMetric) add(d time.Duration) {
	d = max(0, d)
	m.count++
	// 실행 전체 duration 합계는 장기·대규모 실행에서 int64가 넘칠 수 있다.
	// 누적 평균과 고정 크기 카운터만 유지하고 원본 시간열은 저장하지 않는다.
	m.meanNS += (float64(d) - m.meanNS) / float64(m.count)
	m.peak = max(m.peak, d)
	if d <= time.Second {
		m.within1s++
	}
	if d <= 2*time.Second {
		m.within2s++
	}
	if d > 5*time.Second {
		m.over5s++
	}
	// 1µs 이상 2의 거듭제곱 상한 버킷. 원본 시간열을 저장하지 않는다.
	us := uint64(d / time.Microsecond)
	if d%time.Microsecond != 0 {
		us++
	}
	us = max(1, us)
	m.buckets[bits.Len64(us-1)]++
}
func (m durationMetric) summary() DurationSummary {
	s := DurationSummary{Count: m.count, Within1s: m.within1s, Within2s: m.within2s, Over5s: m.over5s}
	if m.count == 0 {
		return s
	}
	s.MeanMS = m.meanNS / float64(time.Millisecond)
	s.MaxMS = float64(m.peak) / float64(time.Millisecond)
	s.P95UpperMS = m.quantileUpper(95)
	s.P99UpperMS = m.quantileUpper(99)
	return s
}

func (m durationMetric) quantileUpper(percent int64) float64 {
	// ceil(count*percent/100)에서 중간 곱셈의 overflow를 피한다.
	target := m.count/100*percent + (m.count%100*percent+99)/100
	var n int64
	for i, count := range m.buckets {
		n += count
		if n >= target {
			return float64(uint64(1)<<i) / 1000
		}
	}
	return 0
}

type PerformanceSummary struct {
	Generation   DurationSummary `json:"generation"`
	Schedule     DurationSummary `json:"schedule"`
	Connect      DurationSummary `json:"connect"`
	Ack          DurationSummary `json:"ack"`
	AckPerSecond float64         `json:"ack_per_second"`
}
type performanceMetrics struct{ generation, schedule, connect, ack durationMetric }

func (m performanceMetrics) summary(ack int, elapsed time.Duration) *PerformanceSummary {
	out := &PerformanceSummary{Generation: m.generation.summary(), Schedule: m.schedule.summary(), Connect: m.connect.summary(), Ack: m.ack.summary()}
	if elapsed > 0 {
		out.AckPerSecond = float64(ack) / elapsed.Seconds()
	}
	return out
}
