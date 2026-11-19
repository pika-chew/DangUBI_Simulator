package main

import (
	"math"
	"sort"
)

type EventEvidence struct {
	Kind         string  `json:"kind"`
	StartMS      int64   `json:"start_ms"`
	EndMS        int64   `json:"end_ms"`
	StartSpeed   float64 `json:"start_speed_kmh"`
	EndSpeed     float64 `json:"end_speed_kmh"`
	Acceleration float64 `json:"acceleration_mps2"`
	Angle        float64 `json:"angle_deg"`
	Lateral      float64 `json:"lateral_mps2"`
	Road         string  `json:"road"`
}
type EventSearchRow struct {
	Sequence int64           `json:"sequence"`
	Message  map[string]any  `json:"message"`
	Evidence []EventEvidence `json:"evidence"`
}
type EventSearchPage struct {
	Rows       []EventSearchRow `json:"rows"`
	NextOffset int64            `json:"next_offset"`
	Total      int64            `json:"total"`
	Scanned    int64            `json:"scanned"`
	Nonzero    int64            `json:"nonzero"`
	Zero       int64            `json:"zero"`
	Missing    int64            `json:"missing"`
	Complete   bool             `json:"complete"`
}

// 내부 기동에서 보고 후보 구간만 만들고, 최종 채택은 실제 자동 생성 em으로 확인한다.
// 결과는 페이지당100건, 후보 확인은5,000건으로 제한한다. 전체 메시지는 보관하지 않는다.
func searchEventMessages(s *ScheduledInput, r Request, first, last int64, message func(int) map[string]any) *EventSearchPage {
	total := max(int64(0), last-first+1)
	start := min(last+1, first+int64(s.Offset))
	out := &EventSearchPage{Rows: []EventSearchRow{}, Total: total}
	cursor := start
	if !s.IncludeEM {
		out.Missing = last + 1 - start
		out.Scanned = out.Missing
		out.NextOffset = total
		out.Complete = true
		return out
	}
	segments := getScenario(r, 0).Segments
	step := float64(s.IntervalMS) / 1000
	type span struct{ low, high int64 }
	ranges := []span{}
	for _, seg := range segments {
		kind := maneuverEvent(seg)
		if kind == "" {
			continue
		}
		lo, hi := int64(math.Ceil(seg.Start/step)), int64(math.Ceil(seg.End/step))
		if completedManeuver(kind) {
			lo = hi
		}
		lo = max(first, int64(1), lo)
		hi = min(last, hi)
		if lo <= hi {
			ranges = append(ranges, span{lo, hi})
		}
	}
	sort.Slice(ranges, func(i, j int) bool { return ranges[i].low < ranges[j].low })
	merged := []span{}
	for _, v := range ranges {
		if len(merged) > 0 && v.low <= merged[len(merged)-1].high+1 {
			merged[len(merged)-1].high = max(merged[len(merged)-1].high, v.high)
		} else {
			merged = append(merged, v)
		}
	}
	evaluated := 0
	for _, v := range merged {
		if v.high < cursor {
			continue
		}
		cursor = max(cursor, v.low)
		for cursor <= v.high {
			seq := cursor
			cursor++
			evaluated++
			m := message(int(seq))
			mask := m["pld"].(map[string]any)["em"].(int)
			if mask != 0 {
				out.Nonzero++
				if s.EventMask == 0 || mask&s.EventMask != 0 {
					t := float64(seq*s.IntervalMS) / 1000
					row := EventSearchRow{Sequence: seq + 1, Message: m, Evidence: []EventEvidence{}}
					from := math.Max(0, t-step)
					idx := sort.Search(len(segments), func(i int) bool { return segments[i].End > from })
					for i := idx; i < len(segments) && segments[i].Start <= t; i++ {
						seg := segments[i]
						kind := maneuverEvent(seg)
						if intervalEventMask([]motionSegment{seg}, from, t) == 0 {
							continue
						}
						end := math.Min(seg.End, t)
						dt := end - seg.Start
						acceleration := (seg.V1 - seg.V0) / (seg.End - seg.Start)
						endSpeed := seg.V0 + acceleration*dt
						row.Evidence = append(row.Evidence, EventEvidence{Kind: kind, StartMS: s.StartMS + int64(math.Round(seg.Start*1000)), EndMS: s.StartMS + int64(math.Round(end*1000)), StartSpeed: seg.V0 * 3.6, EndSpeed: endSpeed * 3.6, Acceleration: acceleration, Angle: math.Abs(seg.Curvature) * (seg.V0 + endSpeed) / 2 * dt * 180 / math.Pi, Lateral: math.Min(seg.V0*seg.V0, endSpeed*endSpeed) * math.Abs(seg.Curvature), Road: seg.Road.Name})
					}
					out.Rows = append(out.Rows, row)
				}
			}
			if len(out.Rows) >= s.Limit || evaluated >= 5000 {
				goto done
			}
		}
	}
	cursor = last + 1
done:
	out.NextOffset = cursor - first
	out.Scanned = cursor - start
	out.Zero = out.Scanned - out.Nonzero
	out.Complete = cursor > last
	return out
}
