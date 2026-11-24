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
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"robber/gateway/bridge"
	"robber/gateway/health"
	gorouter "robber/gateway/router"
	ssotsync "robber/gateway/sync"
)

func TestIPCFrameRoundTrip(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	br := bridge.New("127.0.0.1:0")
	if err := br.Start(ctx); err != nil {
		t.Fatal(err)
	}
	defer br.Close()

	type result struct {
		env bridge.Envelope
		err error
	}
	got := make(chan result, 1)
	go func() {
		conn, err := net.DialTimeout("tcp", br.Addr(), 2*time.Second)
		if err != nil {
			got <- result{err: err}
			return
		}
		defer conn.Close()
		env, err := bridge.ReadFrame(conn)
		if err != nil {
			got <- result{err: err}
			return
		}
		ack := bridge.Envelope{
			Type:    "task_ack",
			ID:      env.ID,
			Payload: bridge.MustPayload(map[string]any{"ok": true}),
		}
		got <- result{env: env, err: bridge.WriteFrame(conn, ack)}
	}()
	if err := br.WaitForPeer(ctx); err != nil {
		t.Fatal(err)
	}
	reply, err := br.Call(ctx, bridge.Envelope{
		Type:    "task",
		Payload: bridge.MustPayload(map[string]any{"prompt": "hello ipc"}),
	})
	if err != nil {
		t.Fatal(err)
	}
	if reply.Type != "task_ack" {
		t.Fatalf("type %s", reply.Type)
	}
	select {
	case row := <-got:
		if row.err != nil {
			t.Fatal(row.err)
		}
		if row.env.Type != "task" {
			t.Fatalf("peer saw %s", row.env.Type)
		}
	case <-ctx.Done():
		t.Fatal("mock peer stalled")
	}
}

func TestGoModelRouterReroutesOnHealthFailure(t *testing.T) {
	db := openTestDB(t)
	hot := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusTooManyRequests)
	}))
	t.Cleanup(hot.Close)
	cold := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	t.Cleanup(cold.Close)
	project, err := db.EnsureProject(t.TempDir(), "route")
	if err != nil {
		t.Fatal(err)
	}
	checker := health.New(db, project, []health.Target{
		{ID: "hot", Provider: "mock-a", URL: hot.URL},
		{ID: "cold", Provider: "mock-b", URL: cold.URL},
	})
	_ = checker.PingAll(context.Background())
	rt := gorouter.New(checker)
	rt.FailLimit = 1
	rt.Cooldown = time.Hour
	rt.SetCatalog([]gorouter.Route{
		{Label: "primary", Provider: "mock-a", Model: "mock-a", HealthID: "hot", Context: 8192},
		{Label: "backup", Provider: "mock-b", Model: "mock-b", HealthID: "cold", Context: 8192},
	})
	rt.SetLabels(gorouter.CODE_GEN, []string{"primary", "backup"})
	dec := rt.Choose(gorouter.CODE_GEN, "implement the ipc bridge")
	if dec.Primary.Label != "backup" {
		t.Fatalf("wanted backup, got %+v isolated=%v", dec.Primary, dec.Isolated)
	}
	if !dec.Rerouted {
		t.Fatal("expected reroute after 429")
	}
	if !contains(dec.Isolated, "primary") {
		t.Fatalf("primary should be isolated: %v", dec.Isolated)
	}
}

func TestSSOTNotifierBroadcastsWebSocket(t *testing.T) {
	app := newLiveApp(t, nil)
	srv := httptest.NewServer(app.Handler())
	t.Cleanup(srv.Close)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go app.Notify.Loop(ctx)

	conn, reader := dialWS(t, srv.URL+"/api/v1/stream?channel=ssot")
	defer conn.Close()
	waitSubscribers(t, app.Hub, 1)

	task, err := app.DB.PutTask(app.Project, "live graph", `{"tag":"gateway"}`)
	if err != nil {
		t.Fatal(err)
	}
	app.Notify.Kick()
	payload := readWSText(t, conn, reader, 2*time.Second)
	if !bytes.Contains(payload, []byte(`"type":"ssot"`)) && !bytes.Contains(payload, []byte(`"type": "ssot"`)) {
		t.Fatalf("ws event %s", payload)
	}
	if !bytes.Contains(payload, []byte(task.ID)) && !bytes.Contains(payload, []byte("task_state")) {
		t.Fatalf("missing task diff %s", payload)
	}
}

func TestPythonBrainIPCRerouteAndNotify(t *testing.T) {
	py := pythonBin(t)
	db := openTestDB(t)
	hot := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	t.Cleanup(hot.Close)
	cold := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(cold.Close)
	project, err := db.EnsureProject(t.TempDir(), "ipc")
	if err != nil {
		t.Fatal(err)
	}
	checker := health.New(db, project, []health.Target{
		{ID: "hot", Provider: "mock-a", URL: hot.URL},
		{ID: "cold", Provider: "mock-b", URL: cold.URL},
	})
	_ = checker.PingAll(context.Background())

	hub := ssotsync.NewHub()
	notify := ssotsync.NewNotifier(db, hub)
	rt := gorouter.New(checker)
	rt.FailLimit = 1
	rt.Cooldown = time.Hour
	rt.SetCatalog([]gorouter.Route{
		{Label: "primary", Provider: "mock-a", Model: "mock-a", HealthID: "hot", Context: 8192},
		{Label: "backup", Provider: "mock-b", Model: "mock-b", HealthID: "cold", Context: 8192},
	})
	rt.SetLabels(gorouter.CODE_GEN, []string{"primary", "backup"})

	ctx, cancel := context.WithTimeout(context.Background(), 12*time.Second)
	defer cancel()
	br := bridge.New("127.0.0.1:0")
	br.On("ssot_mutated", func(env bridge.Envelope) *bridge.Envelope {
		notify.Kick()
		return nil
	})
	if err := br.Start(ctx); err != nil {
		t.Fatal(err)
	}
	defer br.Close()
	go notify.Loop(ctx)

	script := filepath.Join(repoRoot(t), "testdata", "mock_brain.py")
	cmd := exec.CommandContext(ctx, py, script, br.Addr(), db.Path)
	stderr, _ := cmd.StderrPipe()
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = cmd.Process.Kill(); _ = cmd.Wait() })
	go io.Copy(io.Discard, stderr)
	if err := br.WaitForPeer(ctx); err != nil {
		t.Fatal(err)
	}

	app := &App{
		DB:      db,
		Project: project,
		Health:  checker,
		Bridge:  br,
		Router:  rt,
		Hub:     hub,
		Notify:  notify,
		Brain:   "",
	}
	srv := httptest.NewServer(app.Handler())
	t.Cleanup(srv.Close)

	conn, reader := dialWS(t, srv.URL+"/api/v1/stream?channel=ssot")
	defer conn.Close()
	waitSubscribers(t, app.Hub, 1)

	resp, err := http.Post(srv.URL+"/api/v1/agent/task", "application/json", strings.NewReader(
		`{"title":"offload","prompt":"implement router hop","task_type":"CODE_GEN"}`,
	))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("status %d %s", resp.StatusCode, body)
	}
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		t.Fatal(err)
	}
	if out["route"] != "backup" {
		t.Fatalf("gateway route %v body=%s", out["route"], body)
	}
	if out["ipc"] != true {
		t.Fatalf("ipc not accepted: %s", body)
	}
	taskID, _ := out["id"].(string)
	deadline := time.Now().Add(3 * time.Second)
	var sawSSOT bool
	for time.Now().Before(deadline) && !sawSSOT {
		payload, err := tryWSText(conn, reader, 400*time.Millisecond)
		if err != nil {
			continue
		}
		if bytes.Contains(payload, []byte("task_state")) || bytes.Contains(payload, []byte(taskID)) || bytes.Contains(payload, []byte(`"type":"ssot"`)) {
			sawSSOT = true
		}
	}
	if !sawSSOT {
		t.Fatal("websocket missed ssot broadcast")
	}
	row, err := db.GetTask(taskID)
	if err != nil {
		t.Fatal(err)
	}
	if row.Status != "running" && row.Status != "queued" {
		t.Fatalf("task status %s", row.Status)
	}
}

func pythonBin(t *testing.T) string {
	t.Helper()
	for _, name := range []string{"python", "py", "python3"} {
		path, err := exec.LookPath(name)
		if err == nil {
			return path
		}
	}
	t.Skip("python not on PATH")
	return ""
}

func repoRoot(t *testing.T) string {
	t.Helper()
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("caller")
	}
	return filepath.Dir(file)
}

func newLiveApp(t *testing.T, targets []health.Target) *App {
	t.Helper()
	app := newApp(t, targets)
	hub := ssotsync.NewHub()
	app.Hub = hub
	app.Notify = ssotsync.NewNotifier(app.DB, hub)
	app.Router = gorouter.New(app.Health)
	return app
}

func waitSubscribers(t *testing.T, hub *ssotsync.Hub, n int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if hub.Subscribers() >= n {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("subscribers %d want %d", hub.Subscribers(), n)
}

func dialWS(t *testing.T, rawURL string) (net.Conn, *bufio.Reader) {
	t.Helper()
	u := strings.TrimPrefix(rawURL, "http://")
	hostPath := u
	host, path, ok := strings.Cut(u, "/")
	if ok {
		path = "/" + path
	} else {
		host = hostPath
		path = "/"
	}
	conn, err := net.DialTimeout("tcp", host, 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	key := base64.StdEncoding.EncodeToString([]byte("gateway-ssot-key-1"))
	fmt.Fprintf(conn, "GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: %s\r\n\r\n", path, host, key)
	reader := bufio.NewReader(conn)
	status, err := reader.ReadString('\n')
	if err != nil {
		conn.Close()
		t.Fatal(err)
	}
	if !strings.Contains(status, "101") {
		conn.Close()
		t.Fatalf("upgrade %q", status)
	}
	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			conn.Close()
			t.Fatal(err)
		}
		if line == "\r\n" {
			break
		}
	}
	return conn, reader
}

func readWSText(t *testing.T, conn net.Conn, reader *bufio.Reader, d time.Duration) []byte {
	t.Helper()
	payload, err := tryWSText(conn, reader, d)
	if err != nil {
		t.Fatal(err)
	}
	return payload
}

func tryWSText(conn net.Conn, reader *bufio.Reader, d time.Duration) ([]byte, error) {
	_ = conn.SetReadDeadline(time.Now().Add(d))
	hdr := make([]byte, 2)
	if _, err := io.ReadFull(reader, hdr); err != nil {
		return nil, err
	}
	opcode := hdr[0] & 0x0f
	if opcode != 1 && opcode != 2 {
		return nil, fmt.Errorf("opcode %x", hdr[0])
	}
	n := int(hdr[1] & 0x7f)
	if n == 126 {
		ext := make([]byte, 2)
		if _, err := io.ReadFull(reader, ext); err != nil {
			return nil, err
		}
		n = int(ext[0])<<8 | int(ext[1])
	}
	payload := make([]byte, n)
	if _, err := io.ReadFull(reader, payload); err != nil {
		return nil, err
	}
	return payload, nil
}

func contains(items []string, want string) bool {
	for _, item := range items {
		if item == want || strings.Contains(item, want) {
			return true
		}
	}
	return false
}
