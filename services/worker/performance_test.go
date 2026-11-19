package main

import (
	"math"
	"testing"
	"time"
)

func TestDurationThresholdsAndQuantileBounds(t *testing.T) {
	m := durationMetric{}
	for i := 0; i < 95; i++ {
		m.add(time.Second)
	}
	for i := 0; i < 4; i++ {
		m.add(2 * time.Second)
	}
	m.add(5*time.Second + time.Nanosecond)
	s := m.summary()
	if s.Within1s != 95 || s.Within2s != 99 || s.Over5s != 1 || s.Count != 100 {
		t.Fatal("임계값별 건수", s)
	}
	if s.P95UpperMS < 1000 || s.P99UpperMS < 2000 || s.P99UpperMS < s.P95UpperMS {
		t.Fatal("백분위 상한", s)
	}
	boundary := durationMetric{}
	for _, d := range []time.Duration{-1, time.Second, time.Second + 1, 2 * time.Second, 2*time.Second + 1, 5 * time.Second, 5*time.Second + 1} {
		boundary.add(d)
	}
	b := boundary.summary()
	if b.Within1s != 2 || b.Within2s != 4 || b.Over5s != 1 {
		t.Fatal("경계 포함/초과", b)
	}
	if (durationMetric{}).summary().P99UpperMS != 0 {
		t.Fatal("빈 백분위")
	}
}

func TestDurationMeanAvoidsTotalOverflow(t *testing.T) {
	m := durationMetric{}
	d := time.Duration(math.MaxInt64 / 2)
	for i := 0; i < 4; i++ {
		m.add(d)
	}
	s := m.summary()
	want := float64(d) / float64(time.Millisecond)
	if math.Abs(s.MeanMS-want) > .001 || s.P99UpperMS < s.MaxMS || math.IsNaN(s.MeanMS) {
		t.Fatal("시간 합계 overflow 방지", s)
	}
	m.add(time.Duration(math.MaxInt64))
	if m.summary().P99UpperMS < m.summary().MaxMS {
		t.Fatal("최대 duration 버킷 overflow")
	}
}

func TestDurationQuantileRankAvoidsCountOverflow(t *testing.T) {
	const count int64 = math.MaxInt64 - 1
	m := durationMetric{count: count, meanNS: 1000, peak: time.Microsecond, within1s: count, within2s: count}
	m.buckets[0] = count
	m.add(time.Microsecond)
	s := m.summary()
	if s.Count != math.MaxInt64 || s.P95UpperMS != .001 || s.P99UpperMS != .001 || s.MeanMS != .001 {
		t.Fatal("건수 곱셈 overflow 방지", s)
	}
}
