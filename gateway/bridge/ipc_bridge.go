package bridge

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"net"
	"os"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const (
	defaultTCP  = "127.0.0.1:8791"
	queueDepth  = 64
	ioTimeout   = 5 * time.Second
	idleTimeout = 60 * time.Second
	callTimeout = 2 * time.Second
)

// Handler replies to an inbound Python frame. Nil reply means no write.
type Handler func(Envelope) *Envelope

type peer struct {
	conn   net.Conn
	writer *bufio.Writer
	mu     sync.Mutex
}

// Bridge is the Go-side IPC server. Python dials in; Go dispatches tasks.
type Bridge struct {
	Network string
	Address string

	mu       sync.Mutex
	ln       net.Listener
	peers    []*peer
	wait     map[string]chan Envelope
	handlers map[string]Handler
	jobs     chan Envelope
	rr       uint64
	closed   atomic.Bool
}

// DefaultAddr is tcp loopback on Windows, a unix socket under root elsewhere.
func DefaultAddr(root string) string {
	if env := strings.TrimSpace(os.Getenv("GATEWAY_IPC")); env != "" {
		return env
	}
	if runtime.GOOS == "windows" {
		return defaultTCP
	}
	if strings.TrimSpace(root) == "" {
		root = "."
	}
	return "unix:" + strings.TrimRight(strings.ReplaceAll(root, "\\", "/"), "/") + "/.robber/gateway.sock"
}

func New(addr string) *Bridge {
	network, address := SplitNetwork(addr)
	return &Bridge{
		Network: network,
		Address: address,
		wait:    map[string]chan Envelope{},
		handlers: map[string]Handler{
			"ping": func(env Envelope) *Envelope {
				return &Envelope{Type: "pong", Payload: MustPayload(map[string]any{"ok": true})}
			},
		},
		jobs: make(chan Envelope, queueDepth),
	}
}

// SplitNetwork parses tcp:host:port, unix:/path, or a bare host:port.
func SplitNetwork(addr string) (string, string) {
	raw := strings.TrimSpace(addr)
	if raw == "" {
		return "tcp", defaultTCP
	}
	if strings.HasPrefix(raw, "unix:") {
		return "unix", strings.TrimPrefix(raw, "unix:")
	}
	if strings.HasPrefix(raw, "tcp:") {
		return "tcp", strings.TrimPrefix(raw, "tcp:")
	}
	if strings.Contains(raw, "/") || strings.HasSuffix(raw, ".sock") {
		return "unix", raw
	}
	return "tcp", raw
}

func (b *Bridge) On(kind string, h Handler) {
	b.mu.Lock()
	b.handlers[kind] = h
	b.mu.Unlock()
}

func (b *Bridge) Start(ctx context.Context) error {
	if b.Network == "unix" {
		_ = os.MkdirAll(parentDir(b.Address), 0o755)
		_ = os.Remove(b.Address)
	}
	ln, err := net.Listen(b.Network, b.Address)
	if err != nil {
		return err
	}
	b.mu.Lock()
	b.ln = ln
	b.mu.Unlock()
	for i := 0; i < 4; i++ {
		go b.worker(ctx)
	}
	go b.accept(ctx)
	go func() {
		<-ctx.Done()
		_ = b.Close()
	}()
	return nil
}

func (b *Bridge) Addr() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.ln == nil {
		return b.Address
	}
	return b.ln.Addr().String()
}

func (b *Bridge) DialAddr() string {
	if b.Network == "unix" {
		return "unix:" + b.Addr()
	}
	return b.Addr()
}

func (b *Bridge) Peers() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return len(b.peers)
}

func (b *Bridge) WaitForPeer(ctx context.Context) error {
	tick := time.NewTicker(5 * time.Millisecond)
	defer tick.Stop()
	for {
		if b.Peers() > 0 {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-tick.C:
		}
	}
}

func (b *Bridge) Close() error {
	if !b.closed.CompareAndSwap(false, true) {
		return nil
	}
	b.mu.Lock()
	ln := b.ln
	peers := append([]*peer(nil), b.peers...)
	b.peers = nil
	b.ln = nil
	b.mu.Unlock()
	if ln != nil {
		_ = ln.Close()
	}
	for _, p := range peers {
		_ = p.conn.Close()
	}
	if b.Network == "unix" {
		_ = os.Remove(b.Address)
	}
	return nil
}

func (b *Bridge) worker(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case env := <-b.jobs:
			_ = b.WriteAny(env)
		}
	}
}

func (b *Bridge) accept(ctx context.Context) {
	for {
		if ctx.Err() != nil {
			return
		}
		b.mu.Lock()
		ln := b.ln
		b.mu.Unlock()
		if ln == nil {
			return
		}
		conn, err := ln.Accept()
		if err != nil {
			if b.closed.Load() || ctx.Err() != nil {
				return
			}
			time.Sleep(20 * time.Millisecond)
			continue
		}
		go b.serve(ctx, conn)
	}
}

func (b *Bridge) serve(ctx context.Context, conn net.Conn) {
	setNoDelay(conn)
	p := &peer{conn: conn, writer: bufio.NewWriterSize(conn, 4096)}
	b.mu.Lock()
	b.peers = append(b.peers, p)
	b.mu.Unlock()
	defer b.drop(p)

	for {
		if ctx.Err() != nil {
			return
		}
		setIODeadline(conn, idleTimeout)
		env, err := ReadFrame(conn)
		setIODeadline(conn, 0)
		if err != nil {
			return
		}
		if env.Type == "stream_token" || env.Type == "STATE_ACK" || env.Type == "state_ack" {
			if env.Type == "stream_token" && len(bytes.TrimSpace(env.Payload)) == 0 {
				continue
			}
			b.mu.Lock()
			h := b.handlers[env.Type]
			if h == nil {
				h = b.handlers["stream_token"]
			}
			b.mu.Unlock()
			if h != nil {
				if reply := h(env); reply != nil {
					reply.ID = env.ID
					_ = p.write(*reply)
				}
			}
			continue
		}
		if b.complete(env) {
			continue
		}
		b.mu.Lock()
		h := b.handlers[env.Type]
		b.mu.Unlock()
		if h == nil {
			continue
		}
		if reply := h(env); reply != nil {
			reply.ID = env.ID
			_ = p.write(*reply)
		}
	}
}

func (b *Bridge) drop(p *peer) {
	p.mu.Lock()
	if p.writer != nil {
		_ = p.writer.Flush()
	}
	p.mu.Unlock()
	_ = p.conn.Close()
	b.mu.Lock()
	defer b.mu.Unlock()
	out := b.peers[:0]
	for _, cur := range b.peers {
		if cur != p {
			out = append(out, cur)
		}
	}
	b.peers = out
}

func (b *Bridge) complete(env Envelope) bool {
	if env.Type != "task_ack" && env.Type != "pong" && env.Type != "error" {
		return false
	}
	b.mu.Lock()
	ch, ok := b.wait[env.ID]
	if ok {
		delete(b.wait, env.ID)
	}
	b.mu.Unlock()
	if !ok {
		return true
	}
	select {
	case ch <- env:
	default:
	}
	return true
}

// Call sends env to a Python peer and waits for task_ack/pong.
func (b *Bridge) Call(ctx context.Context, env Envelope) (Envelope, error) {
	if b.closed.Load() {
		return Envelope{}, ErrClosed
	}
	if env.ID == "" {
		env.ID = newID()
	}
	if ctx == nil {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(context.Background(), callTimeout)
		defer cancel()
	}
	ch := make(chan Envelope, 1)
	b.mu.Lock()
	b.wait[env.ID] = ch
	b.mu.Unlock()
	defer func() {
		b.mu.Lock()
		delete(b.wait, env.ID)
		b.mu.Unlock()
	}()
	if err := b.WriteAny(env); err != nil {
		return Envelope{}, err
	}
	select {
	case <-ctx.Done():
		return Envelope{}, ctx.Err()
	case reply := <-ch:
		return reply, nil
	}
}

// Notify writes without waiting. Drops if the queue/peer is busy.
func (b *Bridge) Notify(env Envelope) error {
	if env.ID == "" {
		env.ID = newID()
	}
	return b.WriteAny(env)
}

func (b *Bridge) WriteAny(env Envelope) error {
	p := b.nextPeer()
	if p == nil {
		return ErrNoPeer
	}
	return p.write(env)
}

func (b *Bridge) nextPeer() *peer {
	b.mu.Lock()
	defer b.mu.Unlock()
	n := len(b.peers)
	if n == 0 {
		return nil
	}
	i := int(atomic.AddUint64(&b.rr, 1) % uint64(n))
	return b.peers[i]
}

func (p *peer) write(env Envelope) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	setNoDelay(p.conn)
	setIODeadline(p.conn, ioTimeout)
	var err error
	if p.writer != nil {
		err = WriteFrame(p.writer, env)
		if err == nil {
			err = p.writer.Flush()
		}
	} else {
		err = WriteFrame(p.conn, env)
	}
	setIODeadline(p.conn, 0)
	return err
}

func MustPayload(v any) json.RawMessage {
	body, err := json.Marshal(v)
	if err != nil {
		return json.RawMessage("{}")
	}
	return body
}

func newID() string {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		return hex.EncodeToString([]byte(time.Now().Format("150405.00000000")))
	}
	return hex.EncodeToString(b[:])
}

func setNoDelay(conn net.Conn) {
	if tcp, ok := conn.(*net.TCPConn); ok {
		_ = tcp.SetNoDelay(true)
	}
}

func parentDir(path string) string {
	i := strings.LastIndexAny(path, `/\`)
	if i <= 0 {
		return "."
	}
	return path[:i]
}
