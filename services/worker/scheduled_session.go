package main

import (
	"bufio"
	"bytes"
	"container/list"
	"encoding/json"
	"errors"
	"io"
	"runtime"
	"time"
	"unsafe"
)

// 짧은 운행을 차량 수만큼 재사용하되 보유량은 별도의 바이트 예산으로 제한한다.
const scheduleCacheLimit = 1024
const scheduleCacheBytes = 32 * 1024 * 1024

type cachedSchedule struct {
	request Request
	bytes   int
	node    *list.Element
}
type scheduledCache struct {
	entries                         map[ScheduledInput]cachedSchedule
	lru                             list.List
	maxEntries, maxBytes            int
	hits, misses, evictions         int
	bytes                           int
	computeNS, serializeNS, statsNS int64
}

func scheduleKey(s ScheduledInput) ScheduledInput {
	s.SearchEvents = false
	s.EventMask = 0
	s.FromMS = 0
	s.UntilMS = 0
	s.Offset = 0
	s.Limit = 0
	s.ForceEnd = false
	return s
}
func (c *scheduledCache) request(s ScheduledInput, r Request) Request {

	if hit, ok := c.entries[scheduleKey(s)]; ok {
		c.hits++
		c.lru.MoveToBack(hit.node)
		return hit.request
	}
	c.misses++
	return r
}
func (c *scheduledCache) retain(s ScheduledInput, r Request) {
	if c.entries == nil {
		c.entries = map[ScheduledInput]cachedSchedule{}
	}
	key := scheduleKey(s)
	if _, ok := c.entries[key]; ok {
		return
	}
	// 모션/엔진 slice 용량과 가변 문자열을 합산한 캐시 보유량 추정치. 프로세스 RSS 상한은 아니다.
	size := 1024
	if r.Generation.cache != nil {
		for _, p := range r.Generation.cache.plans {
			size += int(unsafe.Sizeof(*p)) + cap(p.Segments)*int(unsafe.Sizeof(motionSegment{}))
			for _, part := range p.Segments {
				size += len(part.Reason) + len(part.Event) + len(part.Road.Name)
			}
			if p.Engine != nil {
				size += int(unsafe.Sizeof(*p.Engine)) + cap(p.Engine.States)*int(unsafe.Sizeof(engineState{}))
			}
		}
	}
	entries, budget := c.limits()
	if size > budget {
		return
	}
	for len(c.entries) >= entries || c.bytes+size > budget {
		oldest := c.lru.Front()
		key := oldest.Value.(ScheduledInput)
		c.bytes -= c.entries[key].bytes
		delete(c.entries, key)
		c.lru.Remove(oldest)
		c.evictions++
	}
	c.entries[key] = cachedSchedule{r, size, c.lru.PushBack(key)}
	c.bytes += size
}
func (c *scheduledCache) limits() (int, int) {
	if c.maxEntries > 0 {
		return c.maxEntries, c.maxBytes
	}
	return scheduleCacheLimit, scheduleCacheBytes
}
func (c *scheduledCache) stats() map[string]any {
	started := time.Now()
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	c.statsNS += time.Since(started).Nanoseconds()
	entries, budget := c.limits()
	return map[string]any{"cache_hits": c.hits, "plan_builds": c.misses, "cache_evictions": c.evictions, "cache_entries": len(c.entries), "cache_estimated_bytes": c.bytes, "cache_max_entries": entries, "cache_max_estimated_bytes": budget, "heap_alloc_bytes": m.HeapAlloc, "heap_sys_bytes": m.HeapSys,
		"worker_compute_ms": float64(c.computeNS) / 1e6, "worker_serialize_ms": float64(c.serializeNS) / 1e6, "worker_stats_ms": float64(c.statsNS) / 1e6}
}
func serveScheduleStream(input io.Reader, w io.Writer) {
	enc := json.NewEncoder(w)
	cache := scheduledCache{}
	if enc.Encode(map[string]any{"id": 0, "ok": true, "stats": cache.stats()}) != nil {
		return
	}
	scanner := bufio.NewScanner(input)
	scanner.Buffer(make([]byte, 4096), 262144)
	last := 0
	for scanner.Scan() {
		if len(bytes.TrimSpace(scanner.Bytes())) == 0 {
			continue
		}
		dec := json.NewDecoder(bytes.NewReader(scanner.Bytes()))
		dec.DisallowUnknownFields()
		var cmd struct {
			ID        int              `json:"id"`
			Command   string           `json:"command"`
			Scheduled *ScheduledInput  `json:"scheduled,omitempty"`
			Pages     []ScheduledInput `json:"pages,omitempty"`
			ByteLimit int              `json:"byte_limit,omitempty"`
		}
		if dec.Decode(&cmd) != nil {
			return
		}
		var extra any
		if dec.Decode(&extra) != io.EOF {
			return
		}
		if cmd.Command == "stop" {
			return
		}
		// 일시정지 경계는 Python 일정 실행기가 관리한다. JobManager의 제어 명령은 응답을 끼워 넣지 않는다.
		if cmd.Command == "pause" || cmd.Command == "resume" {
			continue
		}
		fail := func(message string) { enc.Encode(map[string]any{"id": cmd.ID, "ok": false, "error": message}) }
		if cmd.ID <= last {
			fail("생성 명령 ID 순서를 확인하세요.")
			return
		}
		last = cmd.ID
		if cmd.Command == "close" {
			cache.entries = nil
			cache.bytes = 0
			cache.lru.Init()
			enc.Encode(map[string]any{"id": cmd.ID, "ok": true, "stats": cache.stats()})
			return
		}
		if (cmd.Command != "page" || cmd.Scheduled == nil || len(scanner.Bytes()) > 16384) && cmd.Command != "batch" {
			fail("생성 페이지 명령을 확인하세요.")
			return
		}
		budget := cmd.ByteLimit
		if budget == 0 {
			budget = 1048576
		}
		if budget < 16384 || budget > 1048576 {
			fail("생성 응답 바이트 예산을 확인하세요.")
			return
		}
		reply := map[string]any{"id": cmd.ID, "ok": true}
		if cmd.Command == "batch" {
			if len(cmd.Pages) < 1 || len(cmd.Pages) > 32 || cmd.Scheduled != nil || budget/len(cmd.Pages) < 16384 {
				fail("생성 묶음 예산을 확인하세요.")
				return
			}
			cache.maxEntries, cache.maxBytes = 25000, 512*1024*1024
			pages := make([]map[string]any, 0, len(cmd.Pages))
			for i := range cmd.Pages {
				value, err := executionPage(&cmd.Pages[i], &cache, budget/len(cmd.Pages))
				if err != nil {
					fail(err.Error())
					return
				}
				pages = append(pages, value)
			}
			reply["pages"] = pages
		} else {
			value, err := executionPage(cmd.Scheduled, &cache, budget)
			if err != nil {
				fail(err.Error())
				return
			}
			reply["scheduled"] = value
			reply["next_offset"] = value["next_offset"]
		}
		reply["stats"] = cache.stats()
		started := time.Now()
		raw, err := json.Marshal(reply)
		cache.serializeNS += time.Since(started).Nanoseconds()
		if err != nil || len(raw)+1 > budget {
			fail("응답 바이트 예산을 초과했습니다.")
			return
		}
		if _, err = w.Write(append(raw, '\n')); err != nil {
			return
		}
	}
}

func executionPage(s *ScheduledInput, cache *scheduledCache, budget int) (map[string]any, error) {
	started := time.Now()
	result, err := scheduledPage(Request{Scheduled: s}, cache, true)
	cache.computeNS += time.Since(started).Nanoseconds()
	if err != nil {
		return nil, err
	}
	page := result.Scheduled
	started = time.Now()
	used, keep := 4096, 0
	for _, message := range page.Messages {
		raw, e := json.Marshal(message)
		if e != nil {
			return nil, e
		}
		if used+len(raw)+1 > budget {
			break
		}
		used += len(raw) + 1
		keep++
	}
	if len(page.Messages) > 0 && keep == 0 {
		return nil, errors.New("메시지 한 건이 응답 예산을 초과했습니다.")
	}
	page.Messages = page.Messages[:keep]
	if int64(s.Offset+keep) < page.MicroCount {
		page.Trip = nil
	}
	// 실행 응답에는 그래프와 중간 이벤트 설명을 싣지 않는다. OBD 본문은 조회와 공유한다.
	value := map[string]any{"model": page.Model, "messages": page.Messages, "microtrip_count": page.MicroCount, "trip": page.Trip, "synthetic_trip": page.Synthetic, "final_lat": page.FinalLat, "final_lon": page.FinalLon, "final_odometer_m": page.FinalOdometer, "final_coolant_c": page.FinalCoolant, "final_heading_rad": page.FinalHeading}

	value["next_offset"] = s.Offset + len(page.Messages)
	cache.serializeNS += time.Since(started).Nanoseconds()
	return value, nil
}
