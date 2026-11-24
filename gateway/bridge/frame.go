// Package bridge frames JSON over a local socket between Go and Python.
package bridge

import (
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"net"
	"strings"
	"time"
)

const maxFrame = 1 << 20

// Envelope is one IPC message. Payload must not contain secrets.
type Envelope struct {
	Type    string          `json:"type"`
	ID      string          `json:"id"`
	Payload json.RawMessage `json:"payload,omitempty"`
}

var (
	ErrTooLarge = errors.New("ipc frame exceeds 1 MiB")
	ErrNoPeer   = errors.New("no python peer")
	ErrClosed   = errors.New("ipc bridge closed")
)

// WriteFrame writes a 4-byte big-endian length prefix plus JSON.
func WriteFrame(w io.Writer, env Envelope) error {
	if env.Payload == nil {
		env.Payload = json.RawMessage("{}")
	}
	body, err := json.Marshal(env)
	if err != nil {
		return err
	}
	if len(body) > maxFrame {
		return ErrTooLarge
	}
	var hdr [4]byte
	binary.BigEndian.PutUint32(hdr[:], uint32(len(body)))
	if _, err := w.Write(hdr[:]); err != nil {
		return err
	}
	_, err = w.Write(body)
	return err
}

// ReadFrame reads one length-prefixed JSON envelope.
func ReadFrame(r io.Reader) (Envelope, error) {
	var hdr [4]byte
	if _, err := io.ReadFull(r, hdr[:]); err != nil {
		return Envelope{}, err
	}
	n := binary.BigEndian.Uint32(hdr[:])
	if n == 0 {
		return Envelope{}, errors.New("empty ipc frame")
	}
	if n > maxFrame {
		return Envelope{}, ErrTooLarge
	}
	body := make([]byte, n)
	if _, err := io.ReadFull(r, body); err != nil {
		return Envelope{}, err
	}
	var env Envelope
	if err := json.Unmarshal(body, &env); err != nil {
		return Envelope{}, err
	}
	if strings.TrimSpace(env.Type) == "" {
		return Envelope{}, errors.New("ipc type is empty")
	}
	return env, nil
}

func setIODeadline(conn net.Conn, d time.Duration) {
	if d <= 0 {
		_ = conn.SetDeadline(time.Time{})
		return
	}
	_ = conn.SetDeadline(time.Now().Add(d))
}
