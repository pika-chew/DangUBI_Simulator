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
	Limit        float64 `json:"speed_limit_kmh"`
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
	segments := getScenario(r, 0).Segments
	step := float64(s.IntervalMS) / 1000
	type span struct{ low, high int64 }
	ranges := []span{}
	for _, seg := range segments {
		if a, b, ok := overspeedSpan(seg); ok {
			lo := max(first, int64(1), int64(math.Ceil(a/step)))
			hi := min(last, int64(math.Ceil(b/step)))
			if lo <= hi {
				ranges = append(ranges, span{lo, hi})
			}
		}
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
						bits := intervalEventMask([]motionSegment{seg}, from, t)
						if bits&127 != 0 {
							row.Evidence = append(row.Evidence, searchEvidence(seg, s.StartMS, seg.Start, math.Min(seg.End, t), kind))
						}
						if bits&(1<<8) != 0 {
							a, b, _ := overspeedSpan(seg)
							row.Evidence = append(row.Evidence, searchEvidence(seg, s.StartMS, math.Max(a, from), math.Min(b, t), "과속"))
						}
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

func searchEvidence(seg motionSegment, startMS int64, from, until float64, kind string) EventEvidence {
	acceleration := (seg.V1 - seg.V0) / (seg.End - seg.Start)
	v0 := seg.V0 + acceleration*(from-seg.Start)
	v1 := seg.V0 + acceleration*(until-seg.Start)
	dt := until - from
	return EventEvidence{Kind: kind, StartMS: startMS + int64(math.Round(from*1000)), EndMS: startMS + int64(math.Round(until*1000)), StartSpeed: v0 * 3.6, EndSpeed: v1 * 3.6, Acceleration: acceleration, Angle: math.Abs(seg.Curvature) * (v0 + v1) / 2 * dt * 180 / math.Pi, Lateral: math.Min(v0*v0, v1*v1) * math.Abs(seg.Curvature), Road: seg.Road.Name, Limit: seg.Road.Limit}
}
