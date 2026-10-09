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

func (s *server) extensionOnline() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return !s.lastPoll.IsZero() && time.Since(s.lastPoll) < 30*time.Second
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
		"share_alive":      sharerAlive(),
		"share_post_alive": sharePostAlive(),
		"comment_alive":    commentAlive(),
		"groupscan_alive":  groupsScanAlive(),
		"scripts_ok":       allScriptsOK(),
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

// ---- scheduler ----

func schedulePath() string    { return filepath.Join(cxtDir(), "schedule.json") }
func scheduleCmdPath() string { return filepath.Join(cxtDir(), "schedule.cmd.json") }

func readSchedule() map[string]any { return readJSONMap("schedule.json") }

func schedulerAlive() bool { return procRunning("scheduler.pid", "scheduler.py") }

func ensureScheduler() {
	if schedulerAlive() {
		return
	}
	script := filepath.Join(cxtDir(), "scheduler.py")
	if _, err := os.Stat(script); err != nil {
		return
	}
	if _, err := startDetached(script, "/tmp/cxt_sched.log"); err != nil {
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

// ---- generic job plumbing ----
//
// Los jobs (compartir en grupos, compartir publicacion, comentar, escanear
// grupos) comparten el mismo ciclo de vida: un JSON de estado, un archivo de
// comando, un pid y un log. jobSpec los describe y get/set implementan el
// handler una sola vez.

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

func readJSONMap(name string) map[string]any {
	if name == "" {
		return map[string]any{}
	}
	b, err := os.ReadFile(filepath.Join(cxtDir(), name))
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

func stopJob(pidFile string) {
	if pid, err := readPid(filepath.Join(cxtDir(), pidFile)); err == nil {
		_ = syscall.Kill(pid, syscall.SIGTERM)
	}
	_ = os.Remove(filepath.Join(cxtDir(), pidFile))
}

// startDetached lanza `setsid python3 <script> [args...]` con la salida al log
// indicado y devuelve el pid. El daemon no espera al hijo (lo reapea el SO).
func startDetached(script, logPath string, args ...string) (int, error) {
	argv := append([]string{"python3", script}, args...)
	cmd := exec.Command("setsid", argv...)
	var logf *os.File
	if f, err := os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644); err == nil {
		logf = f
		cmd.Stdout, cmd.Stderr = f, f
	}
	if err := cmd.Start(); err != nil {
		if logf != nil {
			_ = logf.Close()
		}
		return 0, err
	}
	if logf != nil {
		_ = logf.Close()
	}
	return cmd.Process.Pid, nil
}

type jobSpec struct {
	label   string // "comentario" -> "ya hay un comentario en curso"
	script  string // archivo del script en cxtDir()
	status  string // JSON de estado ("" si no aplica)
	cmd     string // JSON de comando ("" si el script no usa --cmd)
	pid     string // pidfile
	log     string // ruta del log
	extra   []string
	summary func(map[string]any) // campos extra para el GET
}

func (j jobSpec) alive() bool { return procRunning(j.pid, j.script) }

func (j jobSpec) get(w http.ResponseWriter) {
	m := readJSONMap(j.status)
	m["alive"] = j.alive()
	if j.summary != nil {
		j.summary(m)
	}
	writeJSON(w, 200, m)
}

func (j jobSpec) set(w http.ResponseWriter, r *http.Request) {
	var st map[string]any
	if j.cmd != "" {
		if err := json.NewDecoder(r.Body).Decode(&st); err != nil {
			http.Error(w, "bad json", http.StatusBadRequest)
			return
		}
	} else {
		_ = json.NewDecoder(r.Body).Decode(&st)
	}
	if st == nil {
		st = map[string]any{}
	}
	if b, ok := st["stop"].(bool); ok && b {
		stopJob(j.pid)
		writeJSON(w, 200, map[string]any{"ok": true, "stopped": true})
		return
	}
	if j.alive() {
		writeJSON(w, 200, map[string]any{"ok": false, "error": "ya hay un " + j.label + " en curso"})
		return
	}
	args := j.extra
	if j.cmd != "" {
		b, _ := json.Marshal(st)
		cmdPath := filepath.Join(cxtDir(), j.cmd)
		if err := os.WriteFile(cmdPath, b, 0o644); err != nil {
			http.Error(w, "no se pudo escribir", http.StatusInternalServerError)
			return
		}
		args = []string{"--cmd", cmdPath}
	}
	pid, err := startDetached(filepath.Join(cxtDir(), j.script), j.log, args...)
	if err != nil {
		writeJSON(w, 200, map[string]any{"ok": false, "error": err.Error()})
		return
	}
	_ = os.WriteFile(filepath.Join(cxtDir(), j.pid), []byte(strconv.Itoa(pid)), 0o644)
	writeJSON(w, 200, map[string]any{"ok": true, "started": true, "pid": pid})
}

var (
	shareSpec = jobSpec{
		label: "share", script: "groups_share.py", status: "share_status.json",
		cmd: "share.cmd.json", pid: "share.pid", log: "/tmp/cxt_share.log",
	}
	sharePostSpec = jobSpec{
		label: "share de publicacion", script: "share_post.py", status: "share_post_status.json",
		cmd: "share_post.cmd.json", pid: "share_post.pid", log: "/tmp/cxt_share_post.log",
	}
	commentSpec = jobSpec{
		label: "comentario", script: "comment.py", status: "comment_status.json",
		cmd: "comment.cmd.json", pid: "comment.pid", log: "/tmp/cxt_comment.log",
	}
	groupsScanSpec = jobSpec{
		label: "escaneo", script: "groups_scan.py", pid: "groups_scan.pid",
		log: "/tmp/cxt_groupscan.log", extra: []string{"--reset"},
		summary: func(m map[string]any) {
			if g, ok := loadGroupsJSON(); ok {
				m["scanned_at"], m["count"], m["categories"] = g["scanned_at"], g["count"], g["categories"]
			}
		},
	}
)

func sharerAlive() bool     { return shareSpec.alive() }
func sharePostAlive() bool  { return sharePostSpec.alive() }
func commentAlive() bool    { return commentSpec.alive() }
func groupsScanAlive() bool { return groupsScanSpec.alive() }

func (s *server) handleShareGet(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	shareSpec.get(w)
}

func (s *server) handleShareSet(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	shareSpec.set(w, r)
}

func (s *server) handleSharePostGet(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	sharePostSpec.get(w)
}

func (s *server) handleSharePostSet(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	sharePostSpec.set(w, r)
}

func (s *server) handleCommentGet(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	commentSpec.get(w)
}

func (s *server) handleCommentSet(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	commentSpec.set(w, r)
}

func (s *server) handleGroupScanGet(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	groupsScanSpec.get(w)
}

func (s *server) handleGroupScanSet(w http.ResponseWriter, r *http.Request) {
	if !s.auth(w, r) {
		return
	}
	groupsScanSpec.set(w, r)
}

// ---- diagnostics ----

func loadGroupsJSON() (map[string]any, bool) {
	b, err := os.ReadFile(filepath.Join(cxtDir(), "groups.json"))
	if err != nil {
		return nil, false
	}
	var g map[string]any
	if json.Unmarshal(b, &g) != nil {
		return nil, false
	}
	return g, true
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
	if g, ok := loadGroupsJSON(); ok {
		if n, ok := g["count"].(float64); ok {
			gc = int(n)
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

// ---- extension transport ----

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

func jobRoute(get, set http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			set(w, r)
		} else {
			get(w, r)
		}
	}
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
	mux.HandleFunc("/schedule", jobRoute(s.handleScheduleGet, s.handleScheduleSet))
	mux.HandleFunc("/sharegroups", jobRoute(s.handleShareGet, s.handleShareSet))
	mux.HandleFunc("/sharepost", jobRoute(s.handleSharePostGet, s.handleSharePostSet))
	mux.HandleFunc("/comment", jobRoute(s.handleCommentGet, s.handleCommentSet))
	mux.HandleFunc("/groupscan", jobRoute(s.handleGroupScanGet, s.handleGroupScanSet))
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
