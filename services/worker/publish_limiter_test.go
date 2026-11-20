package main

import (
	"context"
	"testing"
	"time"
)

func TestPublishLimiterBoundsSlidingWindowAndCoarseTimers(t *testing.T) {
	for _, rate := range []int{1001, 15000, 50000} {
		p := newPublishLimiter(rate)
		start := time.Unix(100, 0)
		now := start
		stamps := make([]time.Time, 0, rate*2)
		head := 0
		same := 0
		previous := time.Time{}
		for len(stamps) < rate*2 {
			if delay := p.allow(now); delay > 0 {
				now = now.Add(max(delay, 500*time.Microsecond))
				continue
			}
			if now.Equal(previous) {
				same++
			} else {
				same = 1
				previous = now
			}
			if same > p.burst {
				t.Fatal("순간 묶음 상한", same, p.burst)
			}
			stamps = append(stamps, now)
			for now.Sub(stamps[head]) >= time.Second {
				head++
			}
			if len(stamps)-head > rate || p.count > rate {
				t.Fatal("이동 1초 허용량 초과", rate)
			}
		}
		if rate == 15000 && now.Sub(start) > 2100*time.Millisecond {
			t.Fatal("타이머 해상도로 처리량 제한", now.Sub(start))
		}
		p.allow(now.Add(10 * time.Second))
		for i := 1; i < p.burst; i++ {
			if p.allow(now.Add(10*time.Second)) != 0 {
				t.Fatal("유휴 후 허용량")
			}
		}
		if p.allow(now.Add(10*time.Second)) == 0 {
			t.Fatal("유휴 시간만큼 무제한 누적")
		}
	}
}

func TestPublishLimiterCancellationDoesNotAdmit(t *testing.T) {
	p := newPublishLimiter(15000)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := p.wait(ctx); err != context.Canceled || p.count != 0 {
		t.Fatal(err, p.count)
	}
}
