// Package ssotsync broadcasts SSOT WAL diffs to local WebSocket clients.
package ssotsync

import (
	"context"
	"crypto/sha1"
	"encoding/base64"
	"encoding/binary"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
)

const hubBuf = 32

// Hub fans JSON events to WebSocket subscribers without blocking writers.
type Hub struct {
	mu   sync.Mutex
	next int
	subs map[int]chan []byte
}

func NewHub() *Hub {
	return &Hub{subs: map[int]chan []byte{}}
}

func (h *Hub) Subscribers() int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.subs)
}

func (h *Hub) Subscribe() (int, <-chan []byte, func()) {
	ch := make(chan []byte, hubBuf)
	h.mu.Lock()
	h.next++
	id := h.next
	h.subs[id] = ch
	h.mu.Unlock()
	return id, ch, func() {
		h.mu.Lock()
		if cur, ok := h.subs[id]; ok {
			delete(h.subs, id)
			close(cur)
		}
		h.mu.Unlock()
	}
}

func (h *Hub) Broadcast(msg []byte) {
	if h == nil || len(msg) == 0 {
		return
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, ch := range h.subs {
		select {
		case ch <- msg:
		default:
		}
	}
}

func (h *Hub) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
		h.ServeWS(w, r)
		return
	}
	h.ServeSSE(w, r)
}

func (h *Hub) ServeSSE(w http.ResponseWriter, r *http.Request) {
	fl, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "stream unsupported", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.WriteHeader(http.StatusOK)
	fl.Flush()
	_, ch, unsub := h.Subscribe()
	defer unsub()
	for {
		select {
		case <-r.Context().Done():
			return
		case msg, ok := <-ch:
			if !ok {
				return
			}
			_, _ = w.Write([]byte("event: ssot\ndata: "))
			_, _ = w.Write(msg)
			_, _ = w.Write([]byte("\n\n"))
			fl.Flush()
		}
	}
}

func (h *Hub) ServeWS(w http.ResponseWriter, r *http.Request) {
	if !headerContains(r.Header, "Connection", "Upgrade") {
		http.Error(w, "expected websocket upgrade", http.StatusBadRequest)
		return
	}
	key := strings.TrimSpace(r.Header.Get("Sec-WebSocket-Key"))
	if key == "" {
		http.Error(w, "missing websocket key", http.StatusBadRequest)
		return
	}
	hj, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "websocket unsupported", http.StatusInternalServerError)
		return
	}
	conn, buf, err := hj.Hijack()
	if err != nil {
		return
	}
	accept := wsAccept(key)
	_, _ = buf.WriteString("HTTP/1.1 101 Switching Protocols\r\n")
	_, _ = buf.WriteString("Upgrade: websocket\r\n")
	_, _ = buf.WriteString("Connection: Upgrade\r\n")
	_, _ = buf.WriteString("Sec-WebSocket-Accept: " + accept + "\r\n\r\n")
	if err := buf.Flush(); err != nil {
		conn.Close()
		return
	}
	defer conn.Close()
	ctx, cancel := context.WithCancel(r.Context())
	defer cancel()
	go drainWS(ctx, conn, cancel)
	_, ch, unsub := h.Subscribe()
	defer unsub()
	for {
		select {
		case <-ctx.Done():
			_ = writeWSClose(conn)
			return
		case msg, ok := <-ch:
			if !ok {
				_ = writeWSClose(conn)
				return
			}
			if err := writeWSText(conn, msg); err != nil {
				return
			}
		}
	}
}

func drainWS(ctx context.Context, conn net.Conn, cancel context.CancelFunc) {
	defer cancel()
	buf := make([]byte, 512)
	for {
		if ctx.Err() != nil {
			return
		}
		_ = conn.SetReadDeadline(time.Now().Add(60 * time.Second))
		_, err := conn.Read(buf)
		if err != nil {
			return
		}
	}
}

func wsAccept(key string) string {
	sum := sha1.Sum([]byte(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"))
	return base64.StdEncoding.EncodeToString(sum[:])
}

func writeWSText(conn net.Conn, payload []byte) error {
	header := []byte{0x81}
	n := len(payload)
	switch {
	case n < 126:
		header = append(header, byte(n))
	case n < 65536:
		header = append(header, 126, byte(n>>8), byte(n))
	default:
		var ext [8]byte
		binary.BigEndian.PutUint64(ext[:], uint64(n))
		header = append(header, 127)
		header = append(header, ext[:]...)
	}
	if _, err := conn.Write(header); err != nil {
		return err
	}
	_, err := conn.Write(payload)
	return err
}

func writeWSClose(conn net.Conn) error {
	_, err := conn.Write([]byte{0x88, 0x00})
	return err
}

func headerContains(h http.Header, key, needle string) bool {
	for _, v := range h.Values(key) {
		if strings.Contains(strings.ToLower(v), strings.ToLower(needle)) {
			return true
		}
	}
	return false
}
