package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"robber/gateway/health"
	"robber/gateway/proxy"
	"robber/gateway/ssot"
)

func TestHealthCheckerWritesSSOT(t *testing.T) {
	db := openTestDB(t)
	okSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	t.Cleanup(okSrv.Close)
	limited := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusTooManyRequests)
	}))
	t.Cleanup(limited.Close)
	down := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	t.Cleanup(down.Close)

	project, err := db.EnsureProject(t.TempDir(), "gateway-test")
	if err != nil {
		t.Fatal(err)
	}
	checker := health.New(db, project, []health.Target{
		{ID: "ok", Provider: "mock", URL: okSrv.URL},
		{ID: "limited", Provider: "mock", URL: limited.URL},
		{ID: "down", Provider: "mock", URL: down.URL},
	})
	results := checker.PingAll(context.Background())
	if len(results) != 3 {
		t.Fatalf("got %d results", len(results))
	}
	var sawOK, saw429, saw500 bool
	for _, row := range results {
		switch row.Target.ID {
		case "ok":
			sawOK = row.OK && row.Status == 200
		case "limited":
			saw429 = row.Limited && row.Status == 429
		case "down":
			saw500 = !row.OK && row.Status == 500
		}
	}
	if !sawOK || !saw429 || !saw500 {
		t.Fatalf("unexpected results: %+v", results)
	}
	rows, err := db.ListHealth(16)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) < 3 {
		t.Fatalf("expected health rows in decisions_ledger, got %d", len(rows))
	}
	snap := checker.Snapshot()
	if len(snap.Targets) != 3 {
		t.Fatalf("snapshot size %d", len(snap.Targets))
	}
	blob := ""
	for _, row := range rows {
		blob += row.Payload + row.Rationale
	}
	if strings.Contains(strings.ToLower(blob), "api_key") {
		t.Fatal("health payload leaked a key field")
	}
}

func TestTaskPersistsAndHealthEndpoint(t *testing.T) {
	app := newApp(t, nil)
	srv := httptest.NewServer(app.Handler())
	t.Cleanup(srv.Close)
	resp, err := http.Post(srv.URL+"/api/v1/agent/task", "application/json", strings.NewReader(
		`{"title":"ping models","prompt":"check latency","task_type":"REASONING"}`,
	))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusAccepted {
		body, _ := io.ReadAll(resp.Body)
		t.Fatalf("status %d %s", resp.StatusCode, body)
	}
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	id, _ := out["id"].(string)
	if id == "" {
		t.Fatal("missing task id")
	}
	task, err := app.DB.GetTask(id)
	if err != nil {
		t.Fatal(err)
	}
	if task.Status != "queued" {
		t.Fatalf("status %s", task.Status)
	}
	if !strings.Contains(task.Payload, "REASONING") {
		t.Fatalf("payload %s", task.Payload)
	}
	hresp, err := http.Get(srv.URL + "/health")
	if err != nil {
		t.Fatal(err)
	}
	defer hresp.Body.Close()
	if hresp.StatusCode != 200 {
		t.Fatalf("health %d", hresp.StatusCode)
	}
}

func TestConcurrentStreamAndHealthNoDeadlock(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fl, ok := w.(http.Flusher)
		if !ok {
			t.Fatal("no flusher")
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(200)
		for i := 0; i < 8; i++ {
			fmt.Fprintf(w, "data: token-%d\n\n", i)
			fl.Flush()
			time.Sleep(5 * time.Millisecond)
		}
	}))
	t.Cleanup(upstream.Close)

	app := newApp(t, []health.Target{{ID: "up", Provider: "mock", URL: upstream.URL}})
	app.Proxy = proxy.New(upstream.URL)
	srv := httptest.NewServer(app.Handler())
	t.Cleanup(srv.Close)

	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	var wg sync.WaitGroup
	var failed atomic.Int32
	const clients = 24
	for i := 0; i < clients; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			req, _ := http.NewRequestWithContext(ctx, http.MethodGet, srv.URL+"/api/v1/stream", nil)
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				failed.Add(1)
				return
			}
			defer resp.Body.Close()
			body, _ := io.ReadAll(resp.Body)
			if !bytes.Contains(body, []byte("token-")) {
				failed.Add(1)
			}
		}()
	}
	wg.Add(1)
	go func() {
		defer wg.Done()
		_ = app.Health.PingAll(ctx)
	}()
	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-ctx.Done():
		t.Fatal("deadlock or timeout under concurrent stream+health")
	}
	if failed.Load() > 0 {
		t.Fatalf("%d stream clients failed", failed.Load())
	}
}

func TestWebSocketUpgradeStreamsTokens(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fl := w.(http.Flusher)
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: hello-ws\n\n")
		fl.Flush()
	}))
	t.Cleanup(upstream.Close)
	app := newApp(t, nil)
	app.Proxy = proxy.New(upstream.URL)
	srv := httptest.NewServer(app.Handler())
	t.Cleanup(srv.Close)

	u, err := url.Parse(srv.URL)
	if err != nil {
		t.Fatal(err)
	}
	conn, err := net.DialTimeout("tcp", u.Host, 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	key := base64.StdEncoding.EncodeToString([]byte("gateway-test-key-1"))
	fmt.Fprintf(conn, "GET /api/v1/stream HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: %s\r\n\r\n", u.Host, key)
	reader := bufio.NewReader(conn)
	status, err := reader.ReadString('\n')
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(status, "101") {
		t.Fatalf("upgrade %q", status)
	}
	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			t.Fatal(err)
		}
		if line == "\r\n" {
			break
		}
	}
	deadline := time.Now().Add(2 * time.Second)
	_ = conn.SetReadDeadline(deadline)
	hdr := make([]byte, 2)
	if _, err := io.ReadFull(reader, hdr); err != nil {
		t.Fatal(err)
	}
	if hdr[0]&0x0f != 1 {
		t.Fatalf("opcode %x", hdr[0])
	}
	n := int(hdr[1] & 0x7f)
	payload := make([]byte, n)
	if _, err := io.ReadFull(reader, payload); err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(payload, []byte("hello-ws")) && !bytes.Contains(payload, []byte("data:")) {
		t.Fatalf("ws payload %q", payload)
	}
}

func TestSsotSnapshotHTTP(t *testing.T) {
	app := newApp(t, nil)
	if _, err := app.DB.PutTask(app.Project, "graph node", `{"prompt":"dag"}`); err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(app.Handler())
	t.Cleanup(srv.Close)
	resp, err := http.Get(srv.URL + "/api/v1/ssot/snapshot")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status %d", resp.StatusCode)
	}
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	tasks, _ := out["tasks"].([]any)
	if len(tasks) < 1 {
		t.Fatalf("expected tasks in snapshot: %+v", out)
	}
}

func TestMemoryUnderConcurrentLoad(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fl := w.(http.Flusher)
		w.Header().Set("Content-Type", "text/event-stream")
		for i := 0; i < 4; i++ {
			fmt.Fprintf(w, "data: %s\n\n", strings.Repeat("x", 64))
			fl.Flush()
		}
	}))
	t.Cleanup(upstream.Close)
	app := newApp(t, []health.Target{{ID: "mem", Provider: "mock", URL: upstream.URL}})
	app.Proxy = proxy.New(upstream.URL)
	srv := httptest.NewServer(app.Handler())
	t.Cleanup(srv.Close)

	runtime.GC()
	var before runtime.MemStats
	runtime.ReadMemStats(&before)

	var wg sync.WaitGroup
	for i := 0; i < 32; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			resp, err := http.Get(srv.URL + "/api/v1/stream")
			if err != nil {
				return
			}
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
		}()
	}
	wg.Wait()
	_ = app.Health.PingAll(context.Background())
	runtime.GC()
	runtime.GC()
	var after runtime.MemStats
	runtime.ReadMemStats(&after)
	mb := float64(after.Alloc) / (1024 * 1024)
	if mb > 50 {
		t.Fatalf("alloc %.1f MB exceeds 50 MB budget (before %.1f)", mb, float64(before.Alloc)/(1024*1024))
	}
}

func openTestDB(t *testing.T) *ssot.DB {
	t.Helper()
	path := filepath.Join(t.TempDir(), "ssot.db")
	db, err := ssot.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return db
}

func newApp(t *testing.T, targets []health.Target) *App {
	t.Helper()
	db := openTestDB(t)
	root := t.TempDir()
	project, err := db.EnsureProject(root, "gw")
	if err != nil {
		t.Fatal(err)
	}
	if targets == nil {
		targets = []health.Target{}
	}
	return &App{
		DB:      db,
		Project: project,
		Health:  health.New(db, project, targets),
		Proxy:   proxy.New(""),
		Root:    root,
		Brain:   "",
	}
}
