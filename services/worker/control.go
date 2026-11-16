package main

import (
	"context"
	"sync"
	"time"
)

type controlKey struct{}
type executionControl struct {
	mu                sync.Mutex
	requested, paused bool
	pausedAt          time.Time
	total             time.Duration
	changed           chan struct{}
}

func newExecutionControl() *executionControl { return &executionControl{changed: make(chan struct{})} }
func controlFrom(ctx context.Context) *executionControl {
	c, _ := ctx.Value(controlKey{}).(*executionControl)
	return c
}
func (c *executionControl) request(pause bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.requested != pause {
		c.requested = pause
		close(c.changed)
		c.changed = make(chan struct{})
	}
}
func (c *executionControl) snapshot() (time.Duration, bool, <-chan struct{}) {
	if c == nil {
		return 0, false, nil
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	total := c.total
	if c.paused {
		total += time.Since(c.pausedAt)
	}
	return total, c.paused, c.changed
}

// 데이터 생성·접속·발행 사이의 안전한 경계에서 정지 완료를 알린다.
// 이미 발행한 메시지는 기존 제한시간 안에서 PUBACK를 기다린다.
func (c *executionControl) gate(ctx context.Context, tick func(bool)) error {
	if c == nil {
		return ctx.Err()
	}
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		c.mu.Lock()
		transition := false
		if c.requested && !c.paused {
			c.paused = true
			c.pausedAt = time.Now()
			transition = true
		}
		if !c.requested && c.paused {
			c.total += time.Since(c.pausedAt)
			c.paused = false
			transition = true
		}
		paused, wake := c.paused, c.changed
		c.mu.Unlock()
		if transition {
			tick(true)
		}
		if !paused {
			return ctx.Err()
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-wake:
		case <-time.After(250 * time.Millisecond):
			tick(false)
		}
	}
}

func waitSchedule(ctx context.Context, target time.Time, tick func(bool)) error {
	control := controlFrom(ctx)
	for {
		if err := control.gate(ctx, tick); err != nil {
			return err
		}
		pause, _, wake := control.snapshot()
		delay := time.Until(target.Add(pause))
		if delay <= 0 {
			return ctx.Err()
		}
		timer := time.NewTimer(min(delay, 250*time.Millisecond))
		select {
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		case <-wake:
			timer.Stop()
		case <-timer.C:
			tick(false)
		}
	}
}

func pauseMilliseconds(c *executionControl) int64 { p, _, _ := c.snapshot(); return p.Milliseconds() }
