// Package ssot talks to the shared Python/Go SQLite ledger (.robber/ssot.db).
package ssot

import (
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

const schemaSQL = `
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_state (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    root_path TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'mixed',
    config_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_state (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    parent_id TEXT,
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL DEFAULT 0,
    queue_order INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    depends_on TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (project_id) REFERENCES project_state(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES task_state(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS agent_state (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    task_id TEXT,
    worker_id TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT 'python',
    phase TEXT NOT NULL DEFAULT 'idle',
    loop_status TEXT NOT NULL DEFAULT '',
    telemetry_json TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions_ledger (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    task_id TEXT,
    branch TEXT NOT NULL DEFAULT 'main',
    parent_id TEXT,
    choice TEXT NOT NULL DEFAULT '',
    rationale TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    version_tag TEXT NOT NULL DEFAULT '',
    hypothesis_delta TEXT NOT NULL DEFAULT '[]',
    confidence_score REAL NOT NULL DEFAULT 0.5,
    payload TEXT NOT NULL DEFAULT '',
    checksum TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    is_final INTEGER NOT NULL DEFAULT 0,
    is_head INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_task_queue
    ON task_state (status, priority DESC, queue_order, created_at);
CREATE INDEX IF NOT EXISTS idx_decisions_version
    ON decisions_ledger (project_id, version_tag, created_at);
CREATE TABLE IF NOT EXISTS execution_artifacts (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    task_id TEXT,
    kind TEXT NOT NULL DEFAULT 'tool',
    name TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    exit_code INTEGER,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ssot_change_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    row_id TEXT NOT NULL,
    op TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ssot_change_seq ON ssot_change_log (seq);
CREATE TRIGGER IF NOT EXISTS trg_task_ai AFTER INSERT ON task_state BEGIN
    INSERT INTO ssot_change_log(table_name, row_id, op, ts)
    VALUES('task_state', NEW.id, 'insert', NEW.updated_at);
END;
CREATE TRIGGER IF NOT EXISTS trg_task_au AFTER UPDATE ON task_state BEGIN
    INSERT INTO ssot_change_log(table_name, row_id, op, ts)
    VALUES('task_state', NEW.id, 'update', NEW.updated_at);
END;
CREATE TRIGGER IF NOT EXISTS trg_agent_ai AFTER INSERT ON agent_state BEGIN
    INSERT INTO ssot_change_log(table_name, row_id, op, ts)
    VALUES('agent_state', NEW.id, 'insert', NEW.updated_at);
END;
CREATE TRIGGER IF NOT EXISTS trg_agent_au AFTER UPDATE ON agent_state BEGIN
    INSERT INTO ssot_change_log(table_name, row_id, op, ts)
    VALUES('agent_state', NEW.id, 'update', NEW.updated_at);
END;
CREATE TRIGGER IF NOT EXISTS trg_art_ai AFTER INSERT ON execution_artifacts BEGIN
    INSERT INTO ssot_change_log(table_name, row_id, op, ts)
    VALUES('execution_artifacts', NEW.id, 'insert', NEW.created_at);
END;
`

// DB is a 4-connection WAL pool matching the Python SsotDB pragmas.
type DB struct {
	SQL  *sql.DB
	Path string
	Root string
}

type Task struct {
	ID       string
	Title    string
	Status   string
	Payload  string
	Priority int
}

type HealthRow struct {
	ID        string
	Choice    string
	Rationale string
	Payload   string
	Created   float64
}

func Open(path string) (*DB, error) {
	if strings.TrimSpace(path) == "" {
		return nil, fmt.Errorf("ssot path is empty")
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(filepath.Dir(abs), 0o755); err != nil {
		return nil, err
	}
	dsn := sqliteDSN(abs)
	sqlDB, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	sqlDB.SetMaxOpenConns(4)
	sqlDB.SetMaxIdleConns(4)
	sqlDB.SetConnMaxLifetime(0)
	if _, err := sqlDB.Exec("PRAGMA foreign_keys=ON"); err != nil {
		sqlDB.Close()
		return nil, err
	}
	db := &DB{SQL: sqlDB, Path: abs, Root: filepath.Dir(filepath.Dir(abs))}
	if err := db.EnsureSchema(); err != nil {
		sqlDB.Close()
		return nil, err
	}
	return db, nil
}

func sqliteDSN(abs string) string {
	slashed := filepath.ToSlash(abs)
	return "file:" + slashed + "?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)&_pragma=foreign_keys(1)&_pragma=synchronous(NORMAL)"
}

func (d *DB) Close() error {
	if d == nil || d.SQL == nil {
		return nil
	}
	return d.SQL.Close()
}

func (d *DB) EnsureSchema() error {
	if _, err := d.SQL.Exec(schemaSQL); err != nil {
		return err
	}
	_, err := d.SQL.Exec(`INSERT INTO schema_meta(key, value) VALUES('gateway', '2')
		ON CONFLICT(key) DO UPDATE SET value=excluded.value`)
	return err
}

func (d *DB) EnsureProject(root, name string) (string, error) {
	if name == "" {
		name = "workspace"
	}
	var id string
	err := d.SQL.QueryRow(
		`SELECT id FROM project_state WHERE root_path=? ORDER BY updated_at DESC LIMIT 1`,
		root,
	).Scan(&id)
	if err == nil && id != "" {
		return id, nil
	}
	if err != nil && err != sql.ErrNoRows {
		return "", err
	}
	id = newID()
	now := nowUnix()
	_, err = d.SQL.Exec(
		`INSERT INTO project_state(id, name, root_path, language, config_json, metadata_json, created_at, updated_at)
		 VALUES(?,?,?,?,?,?,?,?)`,
		id, name, root, "mixed", `{"gateway":true}`, `{"dal":"go-gateway"}`, now, now,
	)
	return id, err
}

func (d *DB) PutTask(projectID, title, payload string) (*Task, error) {
	id := newID()
	now := nowUnix()
	if title == "" {
		title = "gateway task"
	}
	if payload == "" {
		payload = "{}"
	}
	_, err := d.SQL.Exec(
		`INSERT INTO task_state(id, project_id, parent_id, title, status, priority, queue_order, payload_json, depends_on, created_at, updated_at)
		 VALUES(?,?,NULL,?,?,?,?,?,?,?,?)`,
		id, projectID, title, "queued", 0, 0, payload, "[]", now, now,
	)
	if err != nil {
		return nil, err
	}
	return &Task{ID: id, Title: title, Status: "queued", Payload: payload}, nil
}

func (d *DB) GetTask(id string) (*Task, error) {
	row := d.SQL.QueryRow(`SELECT id, title, status, payload_json, priority FROM task_state WHERE id=?`, id)
	t := &Task{}
	if err := row.Scan(&t.ID, &t.Title, &t.Status, &t.Payload, &t.Priority); err != nil {
		return nil, err
	}
	return t, nil
}

func (d *DB) UpsertHealth(projectID, targetID, choice, rationale, payload string, confidence float64) error {
	id := "gw-health-" + sanitizeID(targetID)
	now := nowUnix()
	if confidence <= 0 {
		confidence = 0.5
	}
	_, err := d.SQL.Exec(
		`INSERT INTO decisions_ledger(id, project_id, task_id, branch, parent_id, choice, rationale, created_at,
			version_tag, hypothesis_delta, confidence_score, payload, checksum, summary, is_final, is_head)
		 VALUES(?, ?, NULL, 'health', NULL, ?, ?, ?, 'health', '[]', ?, ?, '', ?, 0, 0)
		 ON CONFLICT(id) DO UPDATE SET choice=excluded.choice, rationale=excluded.rationale,
			created_at=excluded.created_at, payload=excluded.payload, confidence_score=excluded.confidence_score,
			summary=excluded.summary`,
		id, projectID, choice, rationale, now, confidence, payload, choice,
	)
	return err
}

func (d *DB) ListHealth(limit int) ([]HealthRow, error) {
	if limit < 1 {
		limit = 32
	}
	rows, err := d.SQL.Query(
		`SELECT id, choice, rationale, payload, created_at FROM decisions_ledger
		 WHERE branch='health' OR version_tag='health' ORDER BY created_at DESC LIMIT ?`,
		limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []HealthRow
	for rows.Next() {
		var row HealthRow
		if err := rows.Scan(&row.ID, &row.Choice, &row.Rationale, &row.Payload, &row.Created); err != nil {
			return nil, err
		}
		out = append(out, row)
	}
	return out, rows.Err()
}

func (d *DB) PutAgent(projectID, workerID, phase, telemetry string) error {
	id := "gw-agent-" + sanitizeID(workerID)
	now := nowUnix()
	if telemetry == "" {
		telemetry = "{}"
	}
	_, err := d.SQL.Exec(
		`INSERT INTO agent_state(id, project_id, task_id, worker_id, language, phase, loop_status, telemetry_json, updated_at)
		 VALUES(?,?,NULL,?, 'go', ?, ?, ?, ?)
		 ON CONFLICT(id) DO UPDATE SET phase=excluded.phase, loop_status=excluded.loop_status,
			telemetry_json=excluded.telemetry_json, updated_at=excluded.updated_at, project_id=excluded.project_id`,
		id, projectID, workerID, phase, phase, telemetry, now,
	)
	return err
}

func (d *DB) WriteHealthFlag(path string, snapshot any) error {
	if path == "" {
		return nil
	}
	body, err := json.Marshal(snapshot)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, body, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

type ChangeRow struct {
	Seq   int64
	Table string
	RowID string
	Op    string
	Ts    float64
}

func (d *DB) ChangesSince(seq int64, limit int) ([]ChangeRow, error) {
	if limit < 1 {
		limit = 64
	}
	rows, err := d.SQL.Query(
		`SELECT seq, table_name, row_id, op, ts FROM ssot_change_log
		 WHERE seq > ? ORDER BY seq ASC LIMIT ?`,
		seq, limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []ChangeRow
	for rows.Next() {
		var row ChangeRow
		if err := rows.Scan(&row.Seq, &row.Table, &row.RowID, &row.Op, &row.Ts); err != nil {
			return nil, err
		}
		out = append(out, row)
	}
	return out, rows.Err()
}

func (d *DB) MaxChangeSeq() (int64, error) {
	var seq sql.NullInt64
	err := d.SQL.QueryRow(`SELECT MAX(seq) FROM ssot_change_log`).Scan(&seq)
	if err != nil {
		return 0, err
	}
	if !seq.Valid {
		return 0, nil
	}
	return seq.Int64, nil
}

func (d *DB) PruneChanges(keep int) {
	if keep < 256 {
		keep = 256
	}
	_, _ = d.SQL.Exec(
		`DELETE FROM ssot_change_log WHERE seq < (SELECT IFNULL(MAX(seq),0) - ? FROM ssot_change_log)`,
		keep,
	)
}

func (d *DB) PutArtifact(projectID, taskID, kind, name, body string) (string, error) {
	id := newID()
	now := nowUnix()
	if kind == "" {
		kind = "tool"
	}
	_, err := d.SQL.Exec(
		`INSERT INTO execution_artifacts(id, project_id, task_id, kind, name, body, exit_code, created_at)
		 VALUES(?,?,?,?,?,?,NULL,?)`,
		id, projectID, taskID, kind, name, body, now,
	)
	return id, err
}

func (d *DB) UpdateTaskStatus(id, status string) error {
	_, err := d.SQL.Exec(
		`UPDATE task_state SET status=?, updated_at=? WHERE id=?`,
		status, nowUnix(), id,
	)
	return err
}

func (d *DB) TaskSummary(id string) map[string]any {
	row := d.SQL.QueryRow(`SELECT id, title, status, updated_at FROM task_state WHERE id=?`, id)
	var tid, title, status string
	var updated float64
	if err := row.Scan(&tid, &title, &status, &updated); err != nil {
		return map[string]any{"id": id}
	}
	return map[string]any{"id": tid, "title": title, "status": status, "updated_at": updated}
}

func (d *DB) AgentSummary(id string) map[string]any {
	row := d.SQL.QueryRow(
		`SELECT id, worker_id, phase, loop_status, updated_at FROM agent_state WHERE id=?`,
		id,
	)
	var aid, worker, phase, loop string
	var updated float64
	if err := row.Scan(&aid, &worker, &phase, &loop, &updated); err != nil {
		return map[string]any{"id": id}
	}
	return map[string]any{
		"id": aid, "worker_id": worker, "phase": phase,
		"loop_status": loop, "updated_at": updated,
	}
}

func (d *DB) ArtifactSummary(id string) map[string]any {
	row := d.SQL.QueryRow(
		`SELECT id, kind, name, task_id, created_at FROM execution_artifacts WHERE id=?`,
		id,
	)
	var aid, kind, name string
	var taskID sql.NullString
	var created float64
	if err := row.Scan(&aid, &kind, &name, &taskID, &created); err != nil {
		return map[string]any{"id": id}
	}
	out := map[string]any{"id": aid, "kind": kind, "name": name, "created_at": created}
	if taskID.Valid {
		out["task_id"] = taskID.String
	}
	return out
}

func (d *DB) ListTasks(projectID string, limit int) []map[string]any {
	if limit < 1 || limit > 64 {
		limit = 24
	}
	rows, err := d.SQL.Query(
		`SELECT id, title, status, depends_on, updated_at FROM task_state
		 WHERE project_id=? ORDER BY updated_at DESC LIMIT ?`,
		projectID, limit,
	)
	if err != nil {
		return nil
	}
	defer rows.Close()
	var out []map[string]any
	for rows.Next() {
		var id, title, status, depends string
		var updated float64
		if err := rows.Scan(&id, &title, &status, &depends, &updated); err != nil {
			continue
		}
		out = append(out, map[string]any{
			"id": id, "title": title, "status": status,
			"depends_on": depends, "updated_at": updated,
		})
	}
	return out
}

func (d *DB) ListAgents(projectID string, limit int) []map[string]any {
	if limit < 1 || limit > 32 {
		limit = 8
	}
	rows, err := d.SQL.Query(
		`SELECT id, worker_id, phase, loop_status, updated_at FROM agent_state
		 WHERE project_id=? OR project_id IS NULL
		 ORDER BY updated_at DESC LIMIT ?`,
		projectID, limit,
	)
	if err != nil {
		return nil
	}
	defer rows.Close()
	var out []map[string]any
	for rows.Next() {
		var id, worker, phase, loop string
		var updated float64
		if err := rows.Scan(&id, &worker, &phase, &loop, &updated); err != nil {
			continue
		}
		out = append(out, map[string]any{
			"id": id, "worker_id": worker, "phase": phase,
			"loop_status": loop, "updated_at": updated,
		})
	}
	return out
}

func (d *DB) ListDecisions(projectID string, limit int) []map[string]any {
	if limit < 1 || limit > 64 {
		limit = 24
	}
	rows, err := d.SQL.Query(
		`SELECT id, branch, parent_id, choice, version_tag, is_head, is_final, confidence_score, summary
		 FROM decisions_ledger WHERE project_id=? ORDER BY created_at DESC LIMIT ?`,
		projectID, limit,
	)
	if err != nil {
		return nil
	}
	defer rows.Close()
	var out []map[string]any
	for rows.Next() {
		var id, branch, choice, version, summary string
		var parent sql.NullString
		var head, final int
		var conf float64
		if err := rows.Scan(&id, &branch, &parent, &choice, &version, &head, &final, &conf, &summary); err != nil {
			continue
		}
		row := map[string]any{
			"id": id, "branch": branch, "choice": choice, "version_tag": version,
			"is_head": head != 0, "is_final": final != 0, "confidence_score": conf, "summary": summary,
		}
		if parent.Valid {
			row["parent_id"] = parent.String
		}
		out = append(out, row)
	}
	return out
}

func (d *DB) Snapshot(projectID string) map[string]any {
	return map[string]any{
		"project_id": projectID,
		"tasks":      d.ListTasks(projectID, 24),
		"agents":     d.ListAgents(projectID, 8),
		"decisions":  d.ListDecisions(projectID, 24),
	}
}

func newID() string {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		return fmt.Sprintf("%x", time.Now().UnixNano())[:16]
	}
	return hex.EncodeToString(b[:])
}

func nowUnix() float64 {
	return float64(time.Now().UnixNano()) / 1e9
}

func sanitizeID(raw string) string {
	out := strings.Map(func(r rune) rune {
		if r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '-' || r == '_' {
			return r
		}
		return '-'
	}, raw)
	if out == "" {
		return "target"
	}
	if len(out) > 40 {
		return out[:40]
	}
	return out
}
