// Package proxy streams SSE/WebSocket tokens from an upstream LLM or the Python brain.
package proxy

import (
	"bufio"
	"context"
	"crypto/sha1"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

const (
	maxChunk   = 400
	flushEvery = 30 * time.Millisecond
	bufSize    = 32 * 1024
)

var bufPool = sync.Pool{New: func() any {
	b := make([]byte, bufSize)
	return &b
}}

// Proxy copies upstream SSE bytes to the client without decoding JSON tokens.
type Proxy struct {
	Upstream string
	Client   *http.Client
}

func New(upstream string) *Proxy {
	return &Proxy{
		Upstream: strings.TrimRight(upstream, "/"),
		Client: &http.Client{
			Timeout: 0,
			Transport: &http.Transport{
				MaxIdleConns:        16,
				IdleConnTimeout:     30 * time.Second,
				DisableCompression:  true,
				ForceAttemptHTTP2:   false,
				TLSHandshakeTimeout: 5 * time.Second,
			},
		},
	}
}

func (p *Proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
		p.ServeWS(w, r)
		return
	}
	p.ServeSSE(w, r)
}

func (p *Proxy) ServeSSE(w http.ResponseWriter, r *http.Request) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "stream unsupported", http.StatusInternalServerError)
		return
	}
	upstream, err := p.upstreamURL(r)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, upstream, nil)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	req.Header.Set("Accept", "text/event-stream")
	copyHop(req.Header, r.Header)
	resp, err := p.Client.Do(req)
	if err != nil {
		http.Error(w, "upstream unavailable", http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	flusher.Flush()
	_ = copyChunks(r.Context(), &flushWriter{w: w, f: flusher}, resp.Body)
}

func (p *Proxy) ServeWS(w http.ResponseWriter, r *http.Request) {
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
	upstream, err := p.upstreamURL(r)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
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
	ctx := r.Context()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, upstream, nil)
	if err != nil {
		_ = writeWSClose(conn)
		return
	}
	req.Header.Set("Accept", "text/event-stream")
	resp, err := p.Client.Do(req)
	if err != nil {
		_ = writeWSText(conn, "upstream unavailable")
		_ = writeWSClose(conn)
		return
	}
	defer resp.Body.Close()
	_ = pumpSSEToWS(ctx, conn, resp.Body)
}

func (p *Proxy) upstreamURL(r *http.Request) (string, error) {
	raw := p.Upstream
	if q := r.URL.Query().Get("upstream"); q != "" {
		raw = q
	}
	if strings.TrimSpace(raw) == "" {
		return "", errors.New("upstream is empty")
	}
	u, err := url.Parse(raw)
	if err != nil {
		return "", err
	}
	if err := allowUpstream(u); err != nil {
		return "", err
	}
	return u.String(), nil
}

func allowUpstream(u *url.URL) error {
	if u.Scheme != "http" && u.Scheme != "https" {
		return errors.New("upstream must be http(s)")
	}
	host := strings.ToLower(u.Hostname())
	if host == "127.0.0.1" || host == "localhost" || host == "::1" {
		return nil
	}
	ip := net.ParseIP(host)
	if ip != nil && ip.IsLoopback() {
		return nil
	}
	return errors.New("upstream must be loopback")
}

func copyHop(dst, src http.Header) {
	if v := src.Get("Authorization"); v != "" && !strings.Contains(strings.ToLower(v), "bearer") {
		dst.Set("Authorization", v)
	}
}

type flushWriter struct {
	w http.ResponseWriter
	f http.Flusher
}

func (f *flushWriter) Write(p []byte) (int, error) {
	n, err := f.w.Write(p)
	f.f.Flush()
	return n, err
}

func copyChunks(ctx context.Context, dst io.Writer, src io.Reader) error {
	slot := bufPool.Get().(*[]byte)
	defer bufPool.Put(slot)
	chunk := make([]byte, 0, maxChunk)
	last := time.Now()
	reader := bufio.NewReaderSize(src, bufSize)
	buf := *slot
	flush := func() error {
		if len(chunk) == 0 {
			return nil
		}
		_, err := dst.Write(chunk)
		chunk = chunk[:0]
		last = time.Now()
		return err
	}
	for {
		if err := ctx.Err(); err != nil {
			_ = flush()
			return err
		}
		n, err := reader.Read(buf)
		if n > 0 {
			chunk = append(chunk, buf[:n]...)
			if len(chunk) >= maxChunk || time.Since(last) >= flushEvery {
				if werr := flush(); werr != nil {
					return werr
				}
			}
		}
		if err != nil {
			_ = flush()
			if errors.Is(err, io.EOF) {
				return nil
			}
			return err
		}
	}
}

func pumpSSEToWS(ctx context.Context, conn net.Conn, src io.Reader) error {
	slot := bufPool.Get().(*[]byte)
	defer bufPool.Put(slot)
	reader := bufio.NewReaderSize(src, bufSize)
	buf := *slot
	var chunk []byte
	last := time.Now()
	flush := func() error {
		if len(chunk) == 0 {
			return nil
		}
		err := writeWSText(conn, string(chunk))
		chunk = chunk[:0]
		last = time.Now()
		return err
	}
	for {
		if err := ctx.Err(); err != nil {
			_ = flush()
			return err
		}
		n, err := reader.Read(buf)
		if n > 0 {
			chunk = append(chunk, buf[:n]...)
			if len(chunk) >= maxChunk || time.Since(last) >= flushEvery {
				if werr := flush(); werr != nil {
					return werr
				}
			}
		}
		if err != nil {
			_ = flush()
			_ = writeWSClose(conn)
			if errors.Is(err, io.EOF) {
				return nil
			}
			return err
		}
	}
}

func wsAccept(key string) string {
	sum := sha1.Sum([]byte(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"))
	return base64.StdEncoding.EncodeToString(sum[:])
}

func writeWSText(conn net.Conn, text string) error {
	payload := []byte(text)
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
