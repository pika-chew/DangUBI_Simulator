package main

import (
	"bytes"
	"encoding/json"
	"testing"
	"time"
)

func TestManagedProgressThrottlesCountsButKeepsTransitions(t *testing.T) {
	var output bytes.Buffer
	now := time.Unix(1, 0)
	report := managedProgress(&output, func() time.Time { return now })
	r := Result{State: "mqtt_running", ExecutionState: "running"}
	report(r)
	for i := 0; i < 100; i++ {
		r.Puback++
		report(r)
	}
	now = now.Add(250 * time.Millisecond)
	report(r)
	r.ExecutionState = "paused"
	report(r)
	r.ExecutionState = "running"
	report(r)
	r.State = "mqtt_completed"
	report(r)
	decoder := json.NewDecoder(&output)
	var rows []struct {
		Result Result `json:"result"`
	}
	for decoder.More() {
		var row struct {
			Result Result `json:"result"`
		}
		if err := decoder.Decode(&row); err != nil {
			t.Fatal(err)
		}
		rows = append(rows, row)
	}
	if len(rows) != 5 || rows[1].Result.Puback != 100 || rows[2].Result.ExecutionState != "paused" || rows[4].Result.State != "mqtt_completed" {
		t.Fatal(rows)
	}
}
