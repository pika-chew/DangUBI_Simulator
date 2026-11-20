package main

import (
	"context"
	"math"
	"time"
)

// 높은 운영 발행률의 타이머 초과 대기를 줄인다. 최대 1ms 분량의 토큰과
// 이동 1초 창의 개수 제한을 함께 사용한다. publishMu 안에서만 호출한다.
type publishLimiter struct {
	rate, burst, head, count int
	tokens                   float64
	last                     time.Time
	times                    []time.Time
}

func newPublishLimiter(rate int) *publishLimiter {
	return &publishLimiter{rate: rate, burst: max(1, rate/1000), times: make([]time.Time, rate)}
}

func (p *publishLimiter) allow(now time.Time) time.Duration {
	if p.last.IsZero() {
		p.tokens = float64(p.burst)
	} else {
		p.tokens = math.Min(float64(p.burst), p.tokens+math.Max(0, now.Sub(p.last).Seconds())*float64(p.rate))
	}
	p.last = now
	for p.count > 0 && now.Sub(p.times[p.head]) >= time.Second {
		p.head = (p.head + 1) % p.rate
		p.count--
	}
	delay := time.Duration(0)
	if p.tokens < 1 {
		delay = time.Duration(math.Ceil((1 - p.tokens) * float64(time.Second) / float64(p.rate)))
	}
	if p.count == p.rate {
		delay = max(delay, time.Second-now.Sub(p.times[p.head]))
	}
	if delay > 0 {
		return delay
	}
	p.tokens--
	p.times[(p.head+p.count)%p.rate] = now
	p.count++
	return 0
}

func (p *publishLimiter) wait(ctx context.Context) (time.Duration, error) {
	expected := time.Duration(0)
	for {
		if err := ctx.Err(); err != nil {
			return expected, err
		}
		now := time.Now()
		delay := p.allow(now)
		if delay == 0 {
			return expected, nil
		}
		expected += delay
		if err := sleepUntil(ctx, now.Add(delay)); err != nil {
			return expected, err
		}
	}
}
