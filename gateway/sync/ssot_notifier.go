package ssotsync

import (
	"context"
	"encoding/json"
	"os"
	"sync"
	"time"

	"robber/gateway/ssot"
)

const watchEvery = 40 * time.Millisecond

// Event is one SSOT diff batch for UI clients.
type Event struct {
	Type      string           `json:"type"`
	Table     string           `json:"table"`
	Op        string           `json:"op"`
	Rows      []map[string]any `json:"rows"`
	Seq       int64            `json:"seq"`
	Watermark float64          `json:"ts"`
}

type walSig struct {
	dbSize  int64
	walSize int64
	dbMod   int64
	walMod  int64
}

// Notifier watches WAL fingerprints and drains ssot_change_log only on change.
type Notifier struct {
	DB  *ssot.DB
	Hub *Hub

	mu     sync.Mutex
	seq    int64
	sig    walSig
	wake   chan struct{}
	pruned int
}

func NewNotifier(db *ssot.DB, hub *Hub) *Notifier {
	n := &Notifier{DB: db, Hub: hub, wake: make(chan struct{}, 1)}
	if db != nil {
		n.seq, _ = db.MaxChangeSeq()
		n.sig = statWAL(db.Path)
	}
	return n
}

func (n *Notifier) Kick() {
	if n == nil {
		return
	}
	select {
	case n.wake <- struct{}{}:
	default:
	}
}

func (n *Notifier) Loop(ctx context.Context) {
	if n == nil || n.DB == nil {
		return
	}
	tick := time.NewTicker(watchEvery)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-n.wake:
			n.Drain()
		case <-tick.C:
			if n.walChanged() {
				n.Drain()
			}
		}
	}
}

func (n *Notifier) walChanged() bool {
	sig := statWAL(n.DB.Path)
	n.mu.Lock()
	defer n.mu.Unlock()
	if sig == n.sig {
		return false
	}
	n.sig = sig
	return true
}

func (n *Notifier) Drain() []Event {
	if n == nil || n.DB == nil {
		return nil
	}
	n.mu.Lock()
	from := n.seq
	n.mu.Unlock()
	rows, err := n.DB.ChangesSince(from, 128)
	if err != nil || len(rows) == 0 {
		return nil
	}
	grouped := map[string][]map[string]any{}
	ops := map[string]string{}
	var last int64
	var ts float64
	for _, row := range rows {
		last = row.Seq
		ts = row.Ts
		body := n.hydrate(row.Table, row.RowID)
		body["op"] = row.Op
		grouped[row.Table] = append(grouped[row.Table], body)
		ops[row.Table] = row.Op
	}
	n.mu.Lock()
	if last > n.seq {
		n.seq = last
	}
	n.pruned++
	doPrune := n.pruned%32 == 0
	n.mu.Unlock()
	if doPrune {
		n.DB.PruneChanges(2000)
	}
	var out []Event
	now := float64(time.Now().UnixNano()) / 1e9
	for table, list := range grouped {
		ev := Event{
			Type:      "ssot",
			Table:     table,
			Op:        ops[table],
			Rows:      list,
			Seq:       last,
			Watermark: ts,
		}
		if ev.Watermark == 0 {
			ev.Watermark = now
		}
		out = append(out, ev)
		if raw, err := json.Marshal(ev); err == nil && n.Hub != nil {
			n.Hub.Broadcast(raw)
		}
	}
	return out
}

func (n *Notifier) hydrate(table, id string) map[string]any {
	switch table {
	case "task_state":
		return n.DB.TaskSummary(id)
	case "agent_state":
		return n.DB.AgentSummary(id)
	case "execution_artifacts":
		return n.DB.ArtifactSummary(id)
	default:
		return map[string]any{"id": id}
	}
}

func statWAL(path string) walSig {
	var sig walSig
	if st, err := os.Stat(path); err == nil {
		sig.dbSize = st.Size()
		sig.dbMod = st.ModTime().UnixNano()
	}
	if st, err := os.Stat(path + "-wal"); err == nil {
		sig.walSize = st.Size()
		sig.walMod = st.ModTime().UnixNano()
	}
	return sig
}
