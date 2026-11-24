// Robber gateway: low-RAM HTTP/SSE/WebSocket front for the Python brain and SSOT.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"

	"robber/gateway/bridge"
	"robber/gateway/health"
	"robber/gateway/proxy"
	gorouter "robber/gateway/router"
	"robber/gateway/ssot"
	ssotsync "robber/gateway/sync"
)

func main() {
	addr := flag.String("addr", env("GATEWAY_ADDR", "127.0.0.1:8790"), "listen address")
	root := flag.String("root", env("ROBBER_ROOT", "."), "workspace root")
	brain := flag.String("brain", env("ROBBER_BRAIN", "http://127.0.0.1:8787"), "python orchestrator base url")
	ssotPath := flag.String("ssot", os.Getenv("ROBBER_SSOT"), "path to .robber/ssot.db")
	ipcAddr := flag.String("ipc", os.Getenv("GATEWAY_IPC"), "go-python ipc listen addr")
	flag.Parse()

	absRoot, err := filepath.Abs(*root)
	if err != nil {
		log.Fatal(err)
	}
	dbPath := *ssotPath
	if dbPath == "" {
		dbPath = filepath.Join(absRoot, ".robber", "ssot.db")
	}
	db, err := ssot.Open(dbPath)
	if err != nil {
		log.Fatal(err)
	}
	defer db.Close()

	projectID, err := db.EnsureProject(absRoot, filepath.Base(absRoot))
	if err != nil {
		log.Fatal(err)
	}
	targets := healthTargets(*brain)
	checker := health.New(db, projectID, targets)
	checker.FlagPath = filepath.Join(absRoot, ".robber", "gateway-health.json")
	stream := proxy.New(strings.TrimRight(*brain, "/") + "/api/system/stream")
	hub := ssotsync.NewHub()
	notify := ssotsync.NewNotifier(db, hub)
	rt := gorouter.New(checker)
	listenIPC := *ipcAddr
	if listenIPC == "" {
		listenIPC = bridge.DefaultAddr(absRoot)
	}
	br := bridge.New(listenIPC)
	br.On("ssot_mutated", func(env bridge.Envelope) *bridge.Envelope {
		notify.Kick()
		return nil
	})
	br.On("stream_token", func(env bridge.Envelope) *bridge.Envelope {
		if len(bytes.TrimSpace(env.Payload)) == 0 {
			return nil
		}
		hub.Broadcast(env.Payload)
		return nil
	})

	app := &App{
		DB:      db,
		Project: projectID,
		Brain:   strings.TrimRight(*brain, "/"),
		Health:  checker,
		Proxy:   stream,
		Root:    absRoot,
		Bridge:  br,
		Router:  rt,
		Hub:     hub,
		Notify:  notify,
		DashHub: NewDashboardHub(),
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go checker.Loop(ctx)
	go notify.Loop(ctx)
	StartDashboardBroadcast(app.DashHub, db, projectID, checker)
	if err := br.Start(ctx); err != nil {
		log.Printf("ipc offline (%s): %v", listenIPC, err)
	}

	srv := &http.Server{
		Addr:              *addr,
		Handler:           app.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	go func() {
		<-ctx.Done()
		shut, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = srv.Shutdown(shut)
	}()
	log.Printf("robber-gateway %s ssot=%s brain=%s ipc=%s", *addr, dbPath, *brain, br.DialAddr())
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}

// App is the HTTP surface used by main and tests.
type App struct {
	DB      *ssot.DB
	Project string
	Brain   string
	Health  *health.Checker
	Proxy   *proxy.Proxy
	Root    string
	Client  *http.Client
	Bridge  *bridge.Bridge
	Router  *gorouter.Router
	Hub     *ssotsync.Hub
	Notify  *ssotsync.Notifier
	DashHub *DashboardHub
}

func (a *App) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", a.handleHealth)
	mux.HandleFunc("/api/v1/agent/task", a.handleTask)
	mux.HandleFunc("/api/v1/stream", a.handleStream)
	mux.HandleFunc("/api/v1/events", a.handleEvents)
	mux.HandleFunc("/api/v1/health/models", a.handleModels)
	mux.HandleFunc("/api/v1/ssot/snapshot", a.handleSnapshot)
	mux.HandleFunc("/api/v1/dashboard/metrics", a.handleDashboardMetrics)
	if a.DashHub != nil {
		mux.HandleFunc("/ws/dashboard", a.handleDashboardWS(a.DashHub))
	}
	return mux
}

func (a *App) handleHealth(w http.ResponseWriter, r *http.Request) {
	var mem runtime.MemStats
	runtime.ReadMemStats(&mem)
	snap := a.Health.Snapshot()
	peers := 0
	ipc := ""
	if a.Bridge != nil {
		peers = a.Bridge.Peers()
		ipc = a.Bridge.DialAddr()
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":      true,
		"gateway": "robber-gateway",
		"ssot":    a.DB != nil,
		"rss_mb":  float64(mem.Alloc) / (1024 * 1024),
		"models":  snap.Targets,
		"updated": snap.Updated,
		"ipc":     ipc,
		"peers":   peers,
	})
}

func (a *App) handleModels(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, a.Health.Snapshot())
}

type taskIn struct {
	Title    string `json:"title"`
	Prompt   string `json:"prompt"`
	TaskType string `json:"task_type"`
}

func (a *App) handleTask(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST required", http.StatusMethodNotAllowed)
		return
	}
	var in taskIn
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&in); err != nil {
		http.Error(w, "invalid json", http.StatusBadRequest)
		return
	}
	if strings.TrimSpace(in.Prompt) == "" && strings.TrimSpace(in.Title) == "" {
		http.Error(w, "prompt is empty", http.StatusBadRequest)
		return
	}
	title := in.Title
	if title == "" {
		title = in.Prompt
		if len(title) > 80 {
			title = title[:80]
		}
	}
	taskType := strings.TrimSpace(in.TaskType)
	if taskType == "" {
		taskType = "FAST_DRAFT"
	}
	dec := gorouter.Decision{TaskType: taskType, Prompt: in.Prompt}
	if a.Router != nil {
		dec = a.Router.Choose(taskType, in.Prompt)
	}
	payload, _ := json.Marshal(map[string]any{
		"task_type": dec.TaskType,
		"prompt":    dec.Prompt,
		"source":    "gateway",
		"tag":       "gateway",
		"route":     dec,
	})
	task, err := a.DB.PutTask(a.Project, title, string(payload))
	if err != nil {
		http.Error(w, "ssot write failed", http.StatusInternalServerError)
		return
	}
	if a.Notify != nil {
		a.Notify.Kick()
	}
	ipcOK := false
	if a.Bridge != nil && a.Bridge.Peers() > 0 {
		callCtx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		_, err := a.Bridge.Call(callCtx, bridge.Envelope{
			Type: "task",
			Payload: bridge.MustPayload(map[string]any{
				"task_id":   task.ID,
				"prompt":    dec.Prompt,
				"task_type": dec.TaskType,
				"route":     dec,
			}),
		})
		cancel()
		ipcOK = err == nil
	}
	if !ipcOK && a.Brain != "" {
		go a.forwardBrain(in.Prompt, task.ID)
	}
	writeJSON(w, http.StatusAccepted, map[string]any{
		"id":         task.ID,
		"status":     task.Status,
		"task_type":  dec.TaskType,
		"forwarded":  ipcOK || a.Brain != "",
		"ipc":        ipcOK,
		"project_id": a.Project,
		"route":      dec.Primary.Label,
		"rerouted":   dec.Rerouted,
		"isolated":   dec.Isolated,
	})
}

func (a *App) handleStream(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	live := q.Get("channel") == "ssot" || q.Get("events") == "1"
	if live && a.Hub != nil {
		a.Hub.ServeHTTP(w, r)
		return
	}
	if a.Proxy != nil {
		a.Proxy.ServeHTTP(w, r)
		return
	}
	http.Error(w, "stream unavailable", http.StatusBadGateway)
}

func (a *App) handleSnapshot(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "GET required", http.StatusMethodNotAllowed)
		return
	}
	if a.DB == nil {
		http.Error(w, "ssot unavailable", http.StatusServiceUnavailable)
		return
	}
	writeJSON(w, http.StatusOK, a.DB.Snapshot(a.Project))
}

func (a *App) handleEvents(w http.ResponseWriter, r *http.Request) {
	if a.Hub == nil {
		http.Error(w, "events unavailable", http.StatusServiceUnavailable)
		return
	}
	a.Hub.ServeHTTP(w, r)
}

func (a *App) forwardBrain(prompt, taskID string) {
	client := a.Client
	if client == nil {
		client = &http.Client{Timeout: 5 * time.Second}
	}
	body, _ := json.Marshal(map[string]any{
		"type":    "prompt",
		"text":    prompt,
		"task_id": taskID,
	})
	req, err := http.NewRequest(http.MethodPost, a.Brain+"/api/v1/agent/task", bytes.NewReader(body))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		return
	}
	io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
	resp.Body.Close()
}

func healthTargets(brain string) []health.Target {
	raw := os.Getenv("GATEWAY_HEALTH_URLS")
	var out []health.Target
	if raw != "" {
		for i, part := range strings.Split(raw, ",") {
			u := strings.TrimSpace(part)
			if u == "" {
				continue
			}
			out = append(out, health.Target{ID: "t" + itoa(i), Provider: "custom", URL: u})
		}
		return out
	}
	brain = strings.TrimRight(brain, "/")
	if brain != "" {
		out = append(out, health.Target{ID: "brain", Provider: "python", URL: brain + "/api/system/health"})
	}
	out = append(out, health.Target{ID: "ollama", Provider: "ollama", URL: "http://127.0.0.1:11434/api/tags"})
	return out
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

func env(key, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return fallback
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b [8]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}
