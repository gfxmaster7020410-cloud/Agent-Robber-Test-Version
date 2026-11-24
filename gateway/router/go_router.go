// Package router offloads model selection and circuit breaking to the Go gateway.
package router

import (
	"sort"
	"strings"
	"sync"
	"time"

	"robber/gateway/health"
)

const (
	REASONING  = "REASONING"
	FAST_DRAFT = "FAST_DRAFT"
	CODE_GEN   = "CODE_GEN"

	charsPerToken = 4
	clipSuffix    = "\n... (context clipped at token budget)"
)

// Route is a public catalog row. No API keys.
type Route struct {
	Label    string  `json:"label"`
	Provider string  `json:"provider"`
	Model    string  `json:"model"`
	HealthID string  `json:"health_id"`
	CostPerM float64 `json:"cost_per_m"`
	Context  int     `json:"context_length"`
	Protocol string  `json:"protocol"`
	Batch    string  `json:"batch_class"`
}

// Decision is the enriched payload handed to Python.
type Decision struct {
	TaskType  string   `json:"task_type"`
	Primary   Route    `json:"primary"`
	Fallbacks []Route  `json:"fallbacks"`
	Prompt    string   `json:"prompt"`
	Tokens    int      `json:"tokens"`
	Clipped   bool     `json:"clipped"`
	Isolated  []string `json:"isolated"`
	LatencyMS float64  `json:"latency_ms"`
	Rerouted  bool     `json:"rerouted"`
}

type breaker struct {
	fails     int
	openUntil time.Time
}

// Router picks a healthy model from HealthChecker latencies before Python runs.
type Router struct {
	Health    *health.Checker
	Cooldown  time.Duration
	FailLimit int

	mu       sync.Mutex
	catalog  []Route
	byType   map[string][]string
	circuits map[string]*breaker
}

func DefaultCatalog() []Route {
	return []Route{
		{Label: "Claude Opus 4", Provider: "anthropic", Model: "anthropic/claude-sonnet-4-20250514", HealthID: "claude", CostPerM: 15, Context: 200000, Protocol: "ANTHROPIC_COMPATIBLE", Batch: "heavy"},
		{Label: "Gemini Ultra", Provider: "gemini", Model: "gemini/gemini-3.6-flash", HealthID: "gemini", CostPerM: 0.35, Context: 1000000, Protocol: "NATIVE", Batch: "normal"},
		{Label: "Hybrid Multi-LLM", Provider: "hybrid", Model: "groq/openai/gpt-oss-20b", HealthID: "brain", CostPerM: 0.4, Context: 128000, Protocol: "NATIVE", Batch: "normal"},
		{Label: "New API", Provider: "new_api", Model: "openai/gpt-4o-mini", HealthID: "new_api", CostPerM: 0.4, Context: 128000, Protocol: "OPENAI_COMPATIBLE", Batch: "normal"},
		{Label: "GPT-X / Local", Provider: "ollama", Model: "ollama/llama3.2", HealthID: "ollama", CostPerM: 0, Context: 8192, Protocol: "OPENAI_COMPATIBLE", Batch: "eco"},
	}
}

func New(checker *health.Checker) *Router {
	r := &Router{
		Health:    checker,
		Cooldown:  3 * time.Second,
		FailLimit: 2,
		catalog:   DefaultCatalog(),
		circuits:  map[string]*breaker{},
		byType: map[string][]string{
			REASONING:  {"Claude Opus 4", "Gemini Ultra", "Hybrid Multi-LLM", "GPT-X / Local"},
			FAST_DRAFT: {"Gemini Ultra", "New API", "Hybrid Multi-LLM", "GPT-X / Local"},
			CODE_GEN:   {"Claude Opus 4", "Hybrid Multi-LLM", "Gemini Ultra", "New API", "GPT-X / Local"},
		},
	}
	return r
}

func (r *Router) SetCatalog(rows []Route) {
	r.mu.Lock()
	r.catalog = append([]Route(nil), rows...)
	r.mu.Unlock()
}

func (r *Router) SetLabels(taskType string, labels []string) {
	r.mu.Lock()
	if r.byType == nil {
		r.byType = map[string][]string{}
	}
	r.byType[strings.ToUpper(taskType)] = append([]string(nil), labels...)
	r.mu.Unlock()
}

func (r *Router) Choose(taskType, prompt string) Decision {
	kind := strings.ToUpper(strings.TrimSpace(taskType))
	if kind != REASONING && kind != FAST_DRAFT && kind != CODE_GEN {
		kind = FAST_DRAFT
	}
	snap := health.Snapshot{}
	if r.Health != nil {
		snap = r.Health.Snapshot()
	}
	now := time.Now()
	r.mu.Lock()
	labels := append([]string(nil), r.byType[kind]...)
	catalog := append([]Route(nil), r.catalog...)
	r.mu.Unlock()

	type scored struct {
		route   Route
		latency float64
	}
	var picked []scored
	var isolated []string
	for _, label := range labels {
		route, ok := findRoute(catalog, label)
		if !ok {
			continue
		}
		lat, unhealthy, _ := r.inspect(route, snap, now)
		if unhealthy {
			isolated = append(isolated, route.Label)
			continue
		}
		picked = append(picked, scored{route: route, latency: lat})
	}
	sort.SliceStable(picked, func(i, j int) bool {
		if picked[i].latency == picked[j].latency {
			return picked[i].route.CostPerM < picked[j].route.CostPerM
		}
		if picked[i].latency == 0 {
			return false
		}
		if picked[j].latency == 0 {
			return true
		}
		return picked[i].latency < picked[j].latency
	})
	if len(picked) == 0 {
		fallback, ok := findRoute(catalog, "GPT-X / Local")
		if !ok && len(catalog) > 0 {
			fallback = catalog[len(catalog)-1]
			ok = true
		}
		if ok {
			picked = append(picked, scored{route: fallback})
			isolated = append(isolated, fallback.Label+" (degraded)")
		}
	}
	dec := Decision{TaskType: kind, Isolated: isolated, Rerouted: len(isolated) > 0}
	if len(picked) == 0 {
		dec.Prompt, dec.Tokens, dec.Clipped = clipPrompt(prompt, 8192)
		return dec
	}
	dec.Primary = picked[0].route
	dec.LatencyMS = picked[0].latency
	for i := 1; i < len(picked) && i <= 3; i++ {
		dec.Fallbacks = append(dec.Fallbacks, picked[i].route)
	}
	ctx := dec.Primary.Context
	if ctx < 1024 {
		ctx = 8192
	}
	dec.Prompt, dec.Tokens, dec.Clipped = clipPrompt(prompt, ctx)
	return dec
}

func (r *Router) inspect(route Route, snap health.Snapshot, now time.Time) (float64, bool, string) {
	if r.open(route.HealthID, now) || r.open(route.Label, now) {
		return 0, true, "circuit_open"
	}
	row, ok := rollup(snap, route)
	if !ok {
		return 0, false, ""
	}
	if row.Limited > 0 && !row.OK {
		r.Trip(route.HealthID)
		return row.LatencyMS, true, "rate_limited"
	}
	if !row.OK {
		r.Trip(route.HealthID)
		return row.LatencyMS, true, "unhealthy"
	}
	return row.LatencyMS, false, ""
}

func (r *Router) open(id string, now time.Time) bool {
	if id == "" {
		return false
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	br := r.circuits[id]
	if br == nil {
		return false
	}
	return now.Before(br.openUntil)
}

// Trip opens the circuit for a health target or label.
func (r *Router) Trip(id string) {
	if id == "" {
		return
	}
	cool := r.Cooldown
	if cool <= 0 {
		cool = 3 * time.Second
	}
	limit := r.FailLimit
	if limit < 1 {
		limit = 1
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	br := r.circuits[id]
	if br == nil {
		br = &breaker{}
		r.circuits[id] = br
	}
	br.fails++
	if br.fails >= limit {
		br.openUntil = time.Now().Add(cool)
	}
}

func (r *Router) Recover(id string) {
	r.mu.Lock()
	delete(r.circuits, id)
	r.mu.Unlock()
}

func findRoute(rows []Route, label string) (Route, bool) {
	for _, row := range rows {
		if row.Label == label || row.HealthID == label || row.Provider == label {
			return row, true
		}
	}
	return Route{}, false
}

func rollup(snap health.Snapshot, route Route) (health.Rollup, bool) {
	for _, row := range snap.Targets {
		if row.ID == route.HealthID || row.ID == route.Label || strings.EqualFold(row.Provider, route.Provider) {
			return row, true
		}
	}
	return health.Rollup{}, false
}

func clipPrompt(prompt string, ctxTokens int) (string, int, bool) {
	budget := int(float64(ctxTokens)*0.9) - 256
	if budget < 256 {
		budget = 256
	}
	text := prompt
	tokens := estimateTokens(text)
	if tokens <= budget {
		return text, tokens, false
	}
	charBudget := budget*charsPerToken - len(clipSuffix)
	if charBudget < 4 {
		charBudget = 4
	}
	if len(text) > charBudget {
		text = strings.TrimRight(text[:charBudget], " \t\r\n")
	}
	packed := text + clipSuffix
	for estimateTokens(packed) > budget && len(text) > 4 {
		trim := len(text) - charsPerToken
		if trim < 4 {
			trim = 4
		}
		text = strings.TrimRight(text[:trim], " \t\r\n")
		packed = text + clipSuffix
	}
	return packed, estimateTokens(packed), true
}

func estimateTokens(text string) int {
	if text == "" {
		return 0
	}
	n := (len(text) + charsPerToken - 1) / charsPerToken
	if n < 1 {
		return 1
	}
	return n
}
