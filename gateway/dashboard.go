// Dashboard metrics and WebSocket bridge for the web dashboard.
// Exposes /api/v1/dashboard/metrics and /ws/dashboard endpoints that
// stream SSOT state + Go MemStats to connected dashboard clients.
package main

import (
	"encoding/json"
	"log"
	"math"
	"net/http"
	"runtime"
	"sync"
	"time"

	"robber/gateway/health"
	"robber/gateway/ssot"
)

// DashboardMetrics is the JSON payload streamed to dashboard clients.
type DashboardMetrics struct {
	Timestamp float64         `json:"timestamp"`
	RAM       RAMMetrics      `json:"ram"`
	Tasks     TaskMetrics     `json:"tasks"`
	SSOT      SSOTMetrics     `json:"ssot"`
	GoStats   GoRuntimeStats  `json:"go_runtime"`
	Health    json.RawMessage `json:"health,omitempty"`
}

// RAMMetrics reports combined process memory.
type RAMMetrics struct {
	AllocMB    float64 `json:"alloc_mb"`
	HeapMB     float64 `json:"heap_mb"`
	StackMB    float64 `json:"stack_mb"`
	GoRoutines int     `json:"goroutines"`
}

// TaskMetrics reports task_state row counts.
type TaskMetrics struct {
	Total     int            `json:"total"`
	ByStatus  map[string]int `json:"by_status"`
	Queued    int            `json:"queued"`
	Running   int            `json:"running"`
	Completed int            `json:"completed"`
	Failed    int            `json:"failed"`
}

// SSOTMetrics reports SSOT snapshot info.
type SSOTMetrics struct {
	Tables    map[string]int `json:"tables"`
	Version   string         `json:"version"`
	ChangeSeq int64          `json:"change_seq"`
}

// GoRuntimeStats reports Go-specific runtime info.
type GoRuntimeStats struct {
	Version     string  `json:"version"`
	GoRoutines  int     `json:"goroutines"`
	HeapAllocMB float64 `json:"heap_alloc_mb"`
	HeapSysMB   float64 `json:"heap_sys_mb"`
	NumCPU      int     `json:"num_cpu"`
	NumGC       uint32  `json:"num_gc"`
}

// DashboardHub manages WebSocket connections for dashboard streaming.
type DashboardHub struct {
	mu      sync.RWMutex
	clients map[chan []byte]struct{}
}

// NewDashboardHub creates a new dashboard hub.
func NewDashboardHub() *DashboardHub {
	return &DashboardHub{
		clients: make(map[chan []byte]struct{}),
	}
}

// Register adds a client channel. Caller must call Unregister when done.
func (h *DashboardHub) Register() chan []byte {
	ch := make(chan []byte, 16)
	h.mu.Lock()
	h.clients[ch] = struct{}{}
	h.mu.Unlock()
	return ch
}

// Unregister removes a client channel.
func (h *DashboardHub) Unregister(ch chan []byte) {
	h.mu.Lock()
	delete(h.clients, ch)
	h.mu.Unlock()
}

// Broadcast sends a message to all connected clients.
func (h *DashboardHub) Broadcast(data []byte) {
	h.mu.RLock()
	defer h.mu.RUnlock()
	for ch := range h.clients {
		select {
		case ch <- data:
		default:
		}
	}
}

// ClientCount returns the number of connected dashboard clients.
func (h *DashboardHub) ClientCount() int {
	h.mu.RLock()
	defer h.mu.RUnlock()
	return len(h.clients)
}

// CollectMetrics gathers current system metrics for dashboard consumption.
func CollectMetrics(db *ssot.DB, projectID string, checker *health.Checker) DashboardMetrics {
	var mem runtime.MemStats
	runtime.ReadMemStats(&mem)

	metrics := DashboardMetrics{
		Timestamp: float64(time.Now().UnixMilli()) / 1000.0,
		RAM: RAMMetrics{
			AllocMB:    roundMB(float64(mem.Alloc)),
			HeapMB:     roundMB(float64(mem.HeapAlloc)),
			StackMB:    roundMB(float64(mem.StackSys)),
			GoRoutines: runtime.NumGoroutine(),
		},
		Tasks: TaskMetrics{
			ByStatus: make(map[string]int),
		},
		GoStats: GoRuntimeStats{
			Version:     runtime.Version(),
			GoRoutines:  runtime.NumGoroutine(),
			HeapAllocMB: roundMB(float64(mem.HeapAlloc)),
			HeapSysMB:   roundMB(float64(mem.HeapSys)),
			NumCPU:      runtime.NumCPU(),
			NumGC:       mem.NumGC,
		},
	}

	if checker != nil {
		snap := checker.Snapshot()
		data, err := json.Marshal(snap)
		if err == nil {
			metrics.Health = data
		}
	}

	if db != nil {
		snap := db.Snapshot(projectID)
		if tables, ok := snap["tables"].(map[string]int); ok {
			metrics.SSOT.Tables = tables
			for _, count := range tables {
				metrics.Tasks.Total += count
			}
		}
		if ver, ok := snap["version"].(string); ok {
			metrics.SSOT.Version = ver
		}

		if rows := db.ListTasks(projectID, 1000); rows != nil {
			for _, row := range rows {
				status := ""
				if s, ok := row["status"].(string); ok {
					status = s
				}
				metrics.Tasks.ByStatus[status]++
				switch status {
				case "queued":
					metrics.Tasks.Queued++
				case "running":
					metrics.Tasks.Running++
				case "completed":
					metrics.Tasks.Completed++
				case "failed":
					metrics.Tasks.Failed++
				}
			}
		}
	}

	return metrics
}

func roundMB(bytes float64) float64 {
	return math.Round(bytes/(1024*1024)*10) / 10
}

// handleDashboardMetrics serves current metrics as JSON.
func (a *App) handleDashboardMetrics(w http.ResponseWriter, r *http.Request) {
	metrics := CollectMetrics(a.DB, a.Project, a.Health)
	writeJSON(w, http.StatusOK, metrics)
}

// handleDashboardWS upgrades to SSE and streams metrics.
func (a *App) handleDashboardWS(hub *DashboardHub) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		flusher, ok := w.(http.Flusher)
		if !ok {
			http.Error(w, "streaming unsupported", http.StatusInternalServerError)
			return
		}

		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Cache-Control", "no-cache")
		w.Header().Set("Connection", "keep-alive")
		w.Header().Set("Access-Control-Allow-Origin", "*")

		ch := hub.Register()
		defer hub.Unregister(ch)

		ctx := r.Context()
		ticker := time.NewTicker(2 * time.Second)
		defer ticker.Stop()

		metrics := CollectMetrics(a.DB, a.Project, a.Health)
		data, _ := json.Marshal(metrics)
		w.Write(data)
		w.Write([]byte("\n"))
		flusher.Flush()

		for {
			select {
			case <-ctx.Done():
				return
			case msg := <-ch:
				w.Write(msg)
				w.Write([]byte("\n"))
				flusher.Flush()
			case <-ticker.C:
				metrics = CollectMetrics(a.DB, a.Project, a.Health)
				data, _ = json.Marshal(metrics)
				w.Write(data)
				w.Write([]byte("\n"))
				flusher.Flush()
			}
		}
	}
}

// StartDashboardBroadcast starts a goroutine that periodically broadcasts
// metrics to all connected dashboard clients.
func StartDashboardBroadcast(hub *DashboardHub, db *ssot.DB, projectID string, checker *health.Checker) {
	go func() {
		ticker := time.NewTicker(2 * time.Second)
		defer ticker.Stop()
		for range ticker.C {
			if hub.ClientCount() == 0 {
				continue
			}
			metrics := CollectMetrics(db, projectID, checker)
			data, err := json.Marshal(metrics)
			if err != nil {
				log.Printf("[dashboard] marshal error: %v", err)
				continue
			}
			hub.Broadcast(data)
		}
	}()
}
