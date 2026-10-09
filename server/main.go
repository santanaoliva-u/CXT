package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

type cmdMsg struct {
	ID   string          `json:"id"`
	Op   string          `json:"op"`
	Args json.RawMessage `json:"args,omitempty"`
}

type resultMsg struct {
	ID    string          `json:"id"`
	OK    bool            `json:"ok"`
	Data  json.RawMessage `json:"data,omitempty"`
	Error string          `json:"error,omitempty"`
}

type server struct {
	token string

	queue chan cmdMsg
	mu    sync.Mutex
	wait  map[string]chan resultMsg

	lastPoll time.Time

	extEnabled bool
}

func newServer(token string) *server {
	return &server{
		token:      token,
		queue:      make(chan cmdMsg, 64),
		wait:       map[string]chan resultMsg{},
		extEnabled: true,
	}
}

func (s *server) auth(w http.ResponseWriter, r *http.Request) bool {
	origin := r.Header.Get("Origin")
	if origin != "" && !strings.HasPrefix(origin, "chrome-extension://") && !strings.HasPrefix(origin, "moz-extension://") {
		http.Error(w, "forbidden origin", http.StatusForbidden)
		return false
	}
	if s.token != "" {
		got := r.URL.Query().Get("token")
		if got == "" {
			got = r.Header.Get("X-CXT-Token")
		}
		if got != s.token {
			http.Error(w, "bad token", http.StatusUnauthorized)
			return false
		}
	}
	return true
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func randHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(b)
}

func (s *server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	s.mu.Lock()
	online := !s.lastPoll.IsZero() && time.Since(s.lastPoll) < 30*time.Second
	enabled := s.extEnabled
	s.mu.Unlock()
	sch := readSchedule()
	writeJSON(w, 200, map[string]any{
		"ok": true, "version": "0.1.0", "extension_online": online, "enabled": enabled,
		"sched_alive": schedulerAlive(), "sched_enabled": sch["enabled"], "sched_remaining": sch["remaining"],
		"share_alive": sharerAlive(),
		"share_post_alive": sharePostAlive(),
		"comment_alive": commentAlive(),
		"groupscan_alive": groupsScanAlive(),
		"scripts_ok": allScriptsOK(),
	})
}

func (s *server) handleState(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	var st struct {
		Enabled bool `json:"enabled"`
	}
	if err := json.NewDecoder(r.Body).Decode(&st); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	s.mu.Lock()
	s.extEnabled = st.Enabled
	s.lastPoll = time.Now()
	s.mu.Unlock()
	writeJSON(w, 200, map[string]bool{"ok": true, "enabled": st.Enabled})
}

func schedulePath() string    { return filepath.Join(cxtDir(), "schedule.json") }
func scheduleCmdPath() string { return filepath.Join(cxtDir(), "schedule.cmd.json") }

func readSchedule() map[string]any {
	b, err := os.ReadFile(schedulePath())
	if err != nil {
		return map[string]any{}
	}
	var m map[string]any
	if json.Unmarshal(b, &m) != nil {
		return map[string]any{}
	}
	return m
}

// procRunning: true solo si el pidfile apunta a un proceso vivo cuyo cmdline
// contiene marker (evita falsos positivos por zombies o pids reutilizados).
func procRunning(pidFile, marker string) bool {
	b, err := os.ReadFile(filepath.Join(cxtDir(), pidFile))
	if err != nil {
		return false
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(b)))
	if err != nil || pid <= 0 {
		return false
	}
	cb, err := os.ReadFile(filepath.Join("/proc", strconv.Itoa(pid), "cmdline"))
	if err != nil {
		return false
	}
	return strings.Contains(string(cb), marker)
}

func schedulerAlive() bool { return procRunning("scheduler.pid", "scheduler.py") }


func ensureScheduler() {
	if schedulerAlive() {
		return
	}
	script := filepath.Join(cxtDir(), "scheduler.py")
	if _, err := os.Stat(script); err != nil {
		return
	}
	cmd := exec.Command("setsid", "python3", script)
	logf, _ := os.OpenFile("/tmp/cxt_sched.log", os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if logf != nil {
		cmd.Stdout = logf
		cmd.Stderr = logf
	}
	if err := cmd.Start(); err != nil {
		log.Printf("no se pudo arrancar scheduler: %v", err)
	}
}

func (s *server) handleScheduleGet(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	m := readSchedule()
	m["alive"] = schedulerAlive()
	writeJSON(w, 200, m)
}

func (s *server) handleScheduleSet(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	var st map[string]any
	if err := json.NewDecoder(r.Body).Decode(&st); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	b, _ := json.Marshal(st)
	if err := os.WriteFile(scheduleCmdPath(), b, 0o644); err != nil {
		http.Error(w, "no se pudo escribir", http.StatusInternalServerError)
		return
	}
	ensureScheduler()
	writeJSON(w, 200, map[string]any{"ok": true, "pending": true, "alive": schedulerAlive()})
}

func shareStatusPath() string { return filepath.Join(cxtDir(), "share_status.json") }
func shareCmdPath() string    { return filepath.Join(cxtDir(), "share.cmd.json") }
func sharePidPath() string    { return filepath.Join(cxtDir(), "share.pid") }

func readShareStatus() map[string]any {
	b, err := os.ReadFile(shareStatusPath())
	if err != nil {
		return map[string]any{}
	}
	var m map[string]any
	if json.Unmarshal(b, &m) != nil {
		return map[string]any{}
	}
	return m
}

func readPid(path string) (int, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return 0, err
	}
	return strconv.Atoi(strings.TrimSpace(string(b)))
}

func sharerAlive() bool { return procRunning("share.pid", "groups_share.py") }

func (s *server) handleShareGet(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	m := readShareStatus()
	m["alive"] = sharerAlive()
	writeJSON(w, 200, m)
}

func (s *server) handleShareSet(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	var st map[string]any
	if err := json.NewDecoder(r.Body).Decode(&st); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	if b, ok := st["stop"].(bool); ok && b {
		if pid, err := readPid(sharePidPath()); err == nil {
			_ = syscall.Kill(pid, syscall.SIGTERM)
		}
		_ = os.Remove(sharePidPath())
		writeJSON(w, 200, map[string]any{"ok": true, "stopped": true})
		return
	}
	if sharerAlive() {
		writeJSON(w, 200, map[string]any{"ok": false, "error": "ya hay un share en curso"})
		return
	}
	b, _ := json.Marshal(st)
	if err := os.WriteFile(shareCmdPath(), b, 0o644); err != nil {
		http.Error(w, "no se pudo escribir", http.StatusInternalServerError)
		return
	}
	script := filepath.Join(cxtDir(), "groups_share.py")
	cmd := exec.Command("setsid", "python3", script, "--cmd", shareCmdPath())
	logf, _ := os.OpenFile("/tmp/cxt_share.log", os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if logf != nil {
		cmd.Stdout = logf
		cmd.Stderr = logf
	}
	if err := cmd.Start(); err != nil {
		writeJSON(w, 200, map[string]any{"ok": false, "error": err.Error()})
		return
	}
	os.WriteFile(sharePidPath(), []byte(strconv.Itoa(cmd.Process.Pid)), 0o644)
	writeJSON(w, 200, map[string]any{"ok": true, "started": true, "pid": cmd.Process.Pid})
}

func sharePostStatusPath() string { return filepath.Join(cxtDir(), "share_post_status.json") }
func sharePostCmdPath() string    { return filepath.Join(cxtDir(), "share_post.cmd.json") }
func sharePostPidPath() string    { return filepath.Join(cxtDir(), "share_post.pid") }

func readSharePostStatus() map[string]any {
	b, err := os.ReadFile(sharePostStatusPath())
	if err != nil {
		return map[string]any{}
	}
	var m map[string]any
	if json.Unmarshal(b, &m) != nil {
		return map[string]any{}
	}
	return m
}

func sharePostAlive() bool { return procRunning("share_post.pid", "share_post.py") }

func (s *server) handleSharePostGet(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	m := readSharePostStatus()
	m["alive"] = sharePostAlive()
	writeJSON(w, 200, m)
}

func (s *server) handleSharePostSet(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	var st map[string]any
	if err := json.NewDecoder(r.Body).Decode(&st); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	if b, ok := st["stop"].(bool); ok && b {
		if pid, err := readPid(sharePostPidPath()); err == nil {
			_ = syscall.Kill(pid, syscall.SIGTERM)
		}
		_ = os.Remove(sharePostPidPath())
		writeJSON(w, 200, map[string]any{"ok": true, "stopped": true})
		return
	}
	if sharePostAlive() {
		writeJSON(w, 200, map[string]any{"ok": false, "error": "ya hay un share de publicacion en curso"})
		return
	}
	b, _ := json.Marshal(st)
	if err := os.WriteFile(sharePostCmdPath(), b, 0o644); err != nil {
		http.Error(w, "no se pudo escribir", http.StatusInternalServerError)
		return
	}
	script := filepath.Join(cxtDir(), "share_post.py")
	cmd := exec.Command("setsid", "python3", script, "--cmd", sharePostCmdPath())
	logf, _ := os.OpenFile("/tmp/cxt_share_post.log", os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if logf != nil {
		cmd.Stdout = logf
		cmd.Stderr = logf
	}
	if err := cmd.Start(); err != nil {
		writeJSON(w, 200, map[string]any{"ok": false, "error": err.Error()})
		return
	}
	os.WriteFile(sharePostPidPath(), []byte(strconv.Itoa(cmd.Process.Pid)), 0o644)
	writeJSON(w, 200, map[string]any{"ok": true, "started": true, "pid": cmd.Process.Pid})
}

func commentStatusPath() string { return filepath.Join(cxtDir(), "comment_status.json") }
func commentCmdPath() string    { return filepath.Join(cxtDir(), "comment.cmd.json") }
func commentPidPath() string    { return filepath.Join(cxtDir(), "comment.pid") }

func readCommentStatus() map[string]any {
	b, err := os.ReadFile(commentStatusPath())
	if err != nil {
		return map[string]any{}
	}
	var m map[string]any
	if json.Unmarshal(b, &m) != nil {
		return map[string]any{}
	}
	return m
}

func commentAlive() bool { return procRunning("comment.pid", "comment.py") }

func (s *server) handleCommentGet(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	m := readCommentStatus()
	m["alive"] = commentAlive()
	writeJSON(w, 200, m)
}

func (s *server) handleCommentSet(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	var st map[string]any
	if err := json.NewDecoder(r.Body).Decode(&st); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	if b, ok := st["stop"].(bool); ok && b {
		if pid, err := readPid(commentPidPath()); err == nil {
			_ = syscall.Kill(pid, syscall.SIGTERM)
		}
		_ = os.Remove(commentPidPath())
		writeJSON(w, 200, map[string]any{"ok": true, "stopped": true})
		return
	}
	if commentAlive() {
		writeJSON(w, 200, map[string]any{"ok": false, "error": "ya hay un comentario en curso"})
		return
	}
	b, _ := json.Marshal(st)
	if err := os.WriteFile(commentCmdPath(), b, 0o644); err != nil {
		http.Error(w, "no se pudo escribir", http.StatusInternalServerError)
		return
	}
	script := filepath.Join(cxtDir(), "comment.py")
	cmd := exec.Command("setsid", "python3", script, "--cmd", commentCmdPath())
	logf, _ := os.OpenFile("/tmp/cxt_comment.log", os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if logf != nil {
		cmd.Stdout = logf
		cmd.Stderr = logf
	}
	if err := cmd.Start(); err != nil {
		writeJSON(w, 200, map[string]any{"ok": false, "error": err.Error()})
		return
	}
	os.WriteFile(commentPidPath(), []byte(strconv.Itoa(cmd.Process.Pid)), 0o644)
	writeJSON(w, 200, map[string]any{"ok": true, "started": true, "pid": cmd.Process.Pid})
}

func groupsScanPidPath() string { return filepath.Join(cxtDir(), "groups_scan.pid") }

func groupsScanAlive() bool { return procRunning("groups_scan.pid", "groups_scan.py") }

func (s *server) handleGroupScanGet(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	m := map[string]any{"alive": groupsScanAlive()}
	b, err := os.ReadFile(filepath.Join(cxtDir(), "groups.json"))
	if err == nil {
		var g map[string]any
		if json.Unmarshal(b, &g) == nil {
			m["scanned_at"] = g["scanned_at"]
			m["count"] = g["count"]
			m["categories"] = g["categories"]
		}
	}
	writeJSON(w, 200, m)
}

func (s *server) handleGroupScanSet(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	var st map[string]any
	_ = json.NewDecoder(r.Body).Decode(&st)
	if b, ok := st["stop"].(bool); ok && b {
		if pid, err := readPid(groupsScanPidPath()); err == nil {
			_ = syscall.Kill(pid, syscall.SIGTERM)
		}
		_ = os.Remove(groupsScanPidPath())
		writeJSON(w, 200, map[string]any{"ok": true, "stopped": true})
		return
	}
	if groupsScanAlive() {
		writeJSON(w, 200, map[string]any{"ok": false, "error": "ya hay un escaneo en curso"})
		return
	}
	script := filepath.Join(cxtDir(), "groups_scan.py")
	cmd := exec.Command("setsid", "python3", script, "--reset")
	logf, _ := os.OpenFile("/tmp/cxt_groupscan.log", os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if logf != nil {
		cmd.Stdout = logf
		cmd.Stderr = logf
	}
	if err := cmd.Start(); err != nil {
		writeJSON(w, 200, map[string]any{"ok": false, "error": err.Error()})
		return
	}
	os.WriteFile(groupsScanPidPath(), []byte(strconv.Itoa(cmd.Process.Pid)), 0o644)
	writeJSON(w, 200, map[string]any{"ok": true, "started": true, "pid": cmd.Process.Pid})
}

func (s *server) handleSelfTest(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	s.mu.Lock()
	online := !s.lastPoll.IsZero() && time.Since(s.lastPoll) < 30*time.Second
	enabled := s.extEnabled
	s.mu.Unlock()
	gc := 0
	if b, err := os.ReadFile(filepath.Join(cxtDir(), "groups.json")); err == nil {
		var g map[string]any
		if json.Unmarshal(b, &g) == nil {
			if n, ok := g["count"].(float64); ok {
				gc = int(n)
			}
		}
	}
	writeJSON(w, 200, map[string]any{
		"ok": allScriptsOK() && gc > 0, "cxt_dir": cxtDir(), "scripts": scriptsStatus(),
		"groups_count": gc, "enabled": enabled, "extension_online": online,
		"jobs": map[string]bool{
			"scheduler": schedulerAlive(), "share": sharerAlive(), "share_post": sharePostAlive(),
			"comment": commentAlive(), "groupscan": groupsScanAlive(),
		},
	})
}

func (s *server) handleStat(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	s.mu.Lock()
	pending := len(s.wait)
	last := s.lastPoll
	s.mu.Unlock()
	online := !last.IsZero() && time.Since(last) < 30*time.Second
	writeJSON(w, 200, map[string]any{"queued": len(s.queue), "pending": pending, "extension_online": online, "last_poll_ms": last.UnixMilli()})
}

func (s *server) handleNext(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	s.mu.Lock()
	s.lastPoll = time.Now()
	enabled := s.extEnabled
	s.mu.Unlock()
	if !enabled {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	select {
	case c := <-s.queue:
		writeJSON(w, 200, c)
	case <-time.After(25 * time.Second):
		w.WriteHeader(http.StatusNoContent)
	}
}

func (s *server) handleResult(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	var rm resultMsg
	if err := json.NewDecoder(r.Body).Decode(&rm); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	s.mu.Lock()
	ch := s.wait[rm.ID]
	if ch != nil {
		delete(s.wait, rm.ID)
	}
	s.mu.Unlock()
	if ch != nil {
		ch <- rm
	}
	writeJSON(w, 200, map[string]bool{"ok": true})
}

func (s *server) handleCmd(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	var cm cmdMsg
	if err := json.NewDecoder(r.Body).Decode(&cm); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	if cm.Op == "" {
		cm.Op = "ping"
	}
	if cm.ID == "" {
		cm.ID = randHex(8)
	}
	s.mu.Lock()
	enabled := s.extEnabled
	s.mu.Unlock()
	if !enabled {
		writeJSON(w, 200, resultMsg{ID: cm.ID, OK: false, Error: "extension CXT desactivada (kill switch activo): no se ejecuta nada"})
		return
	}
	ch := make(chan resultMsg, 1)
	s.mu.Lock()
	s.wait[cm.ID] = ch
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		delete(s.wait, cm.ID)
		s.mu.Unlock()
	}()

	select {
	case s.queue <- cm:
	case <-time.After(3 * time.Second):
		writeJSON(w, 503, resultMsg{ID: cm.ID, OK: false, Error: "extension no conectada (cola llena)"})
		return
	}

	select {
	case rm := <-ch:
		writeJSON(w, 200, rm)
	case <-time.After(60 * time.Second):
		writeJSON(w, 200, resultMsg{ID: cm.ID, OK: false, Error: "timeout: la extension no respondio"})
	}
}

func cors(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, X-CXT-Token")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		h.ServeHTTP(w, r)
	})
}

func home() string {
	h, err := os.UserHomeDir()
	if err != nil {
		return "."
	}
	return h
}

// cxtDir: directorio de estado/scripts. Override con CXT_DIR (mismo que el resto del stack).
func cxtDir() string {
	if d := os.Getenv("CXT_DIR"); d != "" {
		return d
	}
	return filepath.Join(home(), ".cxt")
}

var requiredScripts = []string{"scheduler.py", "groups_scan.py", "groups_share.py",
	"share_post.py", "comment.py", "clone_publish.py", "news.sh"}

func scriptsStatus() map[string]bool {
	m := map[string]bool{}
	for _, s := range requiredScripts {
		_, err := os.Stat(filepath.Join(cxtDir(), s))
		m[s] = err == nil
	}
	return m
}

func allScriptsOK() bool {
	for _, ok := range scriptsStatus() {
		if !ok {
			return false
		}
	}
	return true
}

func main() {
	token := os.Getenv("CXT_TOKEN")
	tokenPath := filepath.Join(cxtDir(), "token")
	if token == "" {
		if b, err := os.ReadFile(tokenPath); err == nil {
			token = strings.TrimSpace(string(b))
		}
	}
	port := os.Getenv("CXT_PORT")
	if port == "" {
		port = "8799"
	}
	addr := "127.0.0.1:" + port

	s := newServer(token)
	if os.Getenv("CXT_NO_SCHED") == "" {
		ensureScheduler()
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/health", s.handleHealth)
	mux.HandleFunc("/state", s.handleState)
	mux.HandleFunc("/schedule", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			s.handleScheduleSet(w, r)
		} else {
			s.handleScheduleGet(w, r)
		}
	})
	mux.HandleFunc("/sharegroups", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			s.handleShareSet(w, r)
		} else {
			s.handleShareGet(w, r)
		}
	})
	mux.HandleFunc("/sharepost", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			s.handleSharePostSet(w, r)
		} else {
			s.handleSharePostGet(w, r)
		}
	})
	mux.HandleFunc("/comment", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			s.handleCommentSet(w, r)
		} else {
			s.handleCommentGet(w, r)
		}
	})
	mux.HandleFunc("/groupscan", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			s.handleGroupScanSet(w, r)
		} else {
			s.handleGroupScanGet(w, r)
		}
	})
	mux.HandleFunc("/selftest", s.handleSelfTest)
	mux.HandleFunc("/stat", s.handleStat)
	mux.HandleFunc("/next", s.handleNext)
	mux.HandleFunc("/result", s.handleResult)
	mux.HandleFunc("/cmd", s.handleCmd)

	td := "ninguno (solo localhost + rechazo de origen web)"
	if token != "" {
		td = "definido en " + tokenPath
	}
	log.Printf("cxtd v0.1.0 escuchando en http://%s  token=%s", addr, td)
	log.Fatal(http.ListenAndServe(addr, cors(mux)))
}
