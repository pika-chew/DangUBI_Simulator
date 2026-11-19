package main

import "sort"

// 시작 직후의 빈 구간은0. 기존 이상 이벤트만 (이전,현재] 보고에 포함한다.
// 판정과 난수열은 바꾸지 않는다. 지속 중인 이벤트는 겹치는 각 보고 구간에 포함한다.
func intervalEventMask(segments []motionSegment, from, until float64) int {
	if until <= from {
		return 0
	}
	mask := 0
	first := sort.Search(len(segments), func(i int) bool { return segments[i].End > from })
	for i := first; i < len(segments) && segments[i].Start <= until; i++ {
		s := segments[i]
		switch s.Event {
		case "급가속":
			mask |= 1 << 5
		case "급감속":
			mask |= 1 << 4
		case "급회전":
			// 모형의 양의 곡률은 동쪽 기준 반시계 방향, 즉 좌회전이다.
			if s.Curvature > 0 {
				mask |= 1 << 1
			} else if s.Curvature < 0 {
				mask |= 1 << 2
			}
		}
	}
	return mask
}
