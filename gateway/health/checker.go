// Package health runs concurrent provider pings and writes rolling metrics to SSOT.
package health

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"robber/gateway/ssot"
)

const ewmaAlpha = 0.3

// Target is a model/provider endpoint to ping. URLs must not embed secrets.
type Target struct {
	ID       string `json:"id"`
	Provider string `json:"provider"`
	URL      string `json:"url"`
}

// Result is one ping outcome.
type Result struct {
	Target    Target  `json:"target"`
	OK        bool    `json:"ok"`
	Status    int     `json:"status"`
	LatencyMS float64 `json:"latency_ms"`
	Limited   bool    `json:"rate_limited"`
	Error     string  `json:"error,omitempty"`
	When      float64 `json:"ts"`
}

// Snapshot is the in-memory rolling view Python can also read from the health flag file.
type Snapshot struct {
	Targets []Rollup `json:"targets"`
	Updated float64  `json:"updated"`
}

// Rollup is EWMA latency plus error/rate-limit counters.
type Rollup struct {
	ID         string  `json:"id"`
	Provider   string  `json:"provider"`
	URL        string  `json:"url"`
	OK         bool    `json:"ok"`
	LatencyMS  float64 `json:"latency_ms"`
	Errors     int     `json:"errors"`
	Pings      int     `json:"pings"`
	Limited    int     `json:"rate_limits"`
	LastStatus int     `json:"last_status"`
}

// Checker pings targets from goroutines (cap 8) and upserts decisions_ledger.
type Checker struct {
	DB       *ssot.DB
	Project  string
	FlagPath string
	Client   *http.Client
	Interval time.Duration

	mu      sync.Mutex
	targets []Target
	stats   map[string]*Rollup
}

func New(db *ssot.DB, projectID string, targets []Target) *Checker {
	copied := append([]Target(nil), targets...)
	return &Checker{
		DB:       db,
		Project:  projectID,
		Client:   &http.Client{Timeout: 3 * time.Second, Transport: &http.Transport{MaxIdleConns: 8, DisableCompression: true}},
		Interval: 15 * time.Second,
		targets:  copied,
		stats:    map[string]*Rollup{},
	}
}

func (c *Checker) SetTargets(targets []Target) {
	c.mu.Lock()
	c.targets = append([]Target(nil), targets...)
	c.mu.Unlock()
}

func (c *Checker) Targets() []Target {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]Target(nil), c.targets...)
}

func (c *Checker) Snapshot() Snapshot {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := Snapshot{Updated: nowUnix()}
	for _, t := range c.targets {
		if row, ok := c.stats[t.ID]; ok {
			copyRow := *row
			out.Targets = append(out.Targets, copyRow)
		}
	}
	return out
}

func (c *Checker) Loop(ctx context.Context) {
	c.PingAll(ctx)
	if c.Interval <= 0 {
		return
	}
	tick := time.NewTicker(c.Interval)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			c.PingAll(ctx)
		}
	}
}

func (c *Checker) PingAll(ctx context.Context) []Result {
	targets := c.Targets()
	if len(targets) == 0 {
		return nil
	}
	sem := make(chan struct{}, 8)
	out := make(chan Result, len(targets))
	var wg sync.WaitGroup
	for _, t := range targets {
		wg.Add(1)
		go func(t Target) {
			defer wg.Done()
			select {
			case sem <- struct{}{}:
			case <-ctx.Done():
				out <- Result{Target: t, Error: "canceled", When: nowUnix()}
				return
			}
			defer func() { <-sem }()
			out <- c.ping(ctx, t)
		}(t)
	}
	go func() {
		wg.Wait()
		close(out)
	}()
	var results []Result
	for row := range out {
		c.record(row)
		c.persist(row)
		results = append(results, row)
	}
	c.writeFlag()
	return results
}

func (c *Checker) ping(ctx context.Context, t Target) Result {
	row := Result{Target: t, When: nowUnix()}
	if err := validateTarget(t.URL); err != nil {
		row.Error = err.Error()
		return row
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, t.URL, nil)
	if err != nil {
		row.Error = err.Error()
		return row
	}
	req.Header.Set("User-Agent", "robber-gateway-health")
	start := time.Now()
	resp, err := c.Client.Do(req)
	row.LatencyMS = float64(time.Since(start).Microseconds()) / 1000.0
	if err != nil {
		row.Error = err.Error()
		return row
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64*1024))
	row.Status = resp.StatusCode
	row.Limited = resp.StatusCode == 429
	row.OK = resp.StatusCode > 0 && resp.StatusCode < 500 && resp.StatusCode != 429
	return row
}

func (c *Checker) record(row Result) {
	c.mu.Lock()
	defer c.mu.Unlock()
	st := c.stats[row.Target.ID]
	if st == nil {
		st = &Rollup{ID: row.Target.ID, Provider: row.Target.Provider, URL: row.Target.URL}
		c.stats[row.Target.ID] = st
	}
	st.Pings++
	st.LastStatus = row.Status
	st.OK = row.OK
	if row.Error != "" || !row.OK {
		st.Errors++
	}
	if row.Limited {
		st.Limited++
	}
	if st.LatencyMS == 0 {
		st.LatencyMS = row.LatencyMS
	} else {
		st.LatencyMS = ewmaAlpha*row.LatencyMS + (1-ewmaAlpha)*st.LatencyMS
	}
}

func (c *Checker) persist(row Result) {
	if c.DB == nil {
		return
	}
	c.mu.Lock()
	st := c.stats[row.Target.ID]
	var copyRow Rollup
	if st != nil {
		copyRow = *st
	}
	c.mu.Unlock()
	payload, _ := json.Marshal(map[string]any{
		"id":          row.Target.ID,
		"provider":    row.Target.Provider,
		"url":         row.Target.URL,
		"ok":          row.OK,
		"status":      row.Status,
		"latency_ms":  round1(row.LatencyMS),
		"ewma_ms":     round1(copyRow.LatencyMS),
		"errors":      copyRow.Errors,
		"pings":       copyRow.Pings,
		"rate_limits": copyRow.Limited,
	})
	choice := "health " + row.Target.ID
	if row.OK {
		choice += " ok"
	} else if row.Limited {
		choice += " 429"
	} else {
		choice += " down"
	}
	rationale := row.Error
	if rationale == "" {
		rationale = row.Target.Provider
	}
	conf := 0.85
	if !row.OK {
		conf = 0.25
	}
	_ = c.DB.UpsertHealth(c.Project, row.Target.ID, choice, rationale, string(payload), conf)
}

func (c *Checker) writeFlag() {
	if c.DB == nil {
		return
	}
	snap := c.Snapshot()
	_ = c.DB.PutAgent(c.Project, "gateway-health", "running", mustJSON(snap))
	if c.FlagPath != "" {
		_ = c.DB.WriteHealthFlag(c.FlagPath, snap)
	}
}

func validateTarget(raw string) error {
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" {
		return errOr("invalid health url")
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return errOr("health url must be http(s)")
	}
	host := strings.ToLower(u.Hostname())
	if strings.Contains(strings.ToLower(raw), "api_key=") || strings.Contains(strings.ToLower(raw), "key=") {
		return errOr("health url must not embed secrets")
	}
	if strings.Contains(host, "ollama") || strings.HasSuffix(host, "11434") || host == "127.0.0.1" || host == "localhost" || host == "::1" {
		return nil
	}
	if ip := net.ParseIP(host); ip != nil && ip.IsLoopback() {
		return nil
	}
	return nil
}

type staticError string

func (e staticError) Error() string { return string(e) }

func errOr(msg string) error { return staticError(msg) }

func nowUnix() float64 { return float64(time.Now().UnixNano()) / 1e9 }

func round1(v float64) float64 { return float64(int(v*10+0.5)) / 10 }

func mustJSON(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		return "{}"
	}
	return string(b)
}
