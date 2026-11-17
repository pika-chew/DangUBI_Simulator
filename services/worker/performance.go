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
}
type durationMetric struct {
	count       int64
	total, peak time.Duration
	buckets     [64]int64
}

func (m *durationMetric) add(d time.Duration) {
	d = max(0, d)
	m.count++
	m.total += d
	m.peak = max(m.peak, d)
	// 1µs 이상 2의 거듭제곱 상한 버킷. 원본 시간열을 저장하지 않는다.
	us := uint64(max(1, (d+time.Microsecond-1)/time.Microsecond))
	m.buckets[bits.Len64(us-1)]++
}
func (m durationMetric) summary() DurationSummary {
	s := DurationSummary{Count: m.count}
	if m.count == 0 {
		return s
	}
	s.MeanMS = float64(m.total) / float64(time.Millisecond) / float64(m.count)
	s.MaxMS = float64(m.peak) / float64(time.Millisecond)
	target := (m.count*95 + 99) / 100
	var n int64
	for i, count := range m.buckets {
		n += count
		if n >= target {
			s.P95UpperMS = float64(uint64(1)<<i) / 1000
			break
		}
	}
	return s
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
