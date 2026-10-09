package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

type resultMsg struct {
	ID    string          `json:"id"`
	OK    bool            `json:"ok"`
	Data  json.RawMessage `json:"data,omitempty"`
	Error string          `json:"error,omitempty"`
}

const usage = `cxt — controla tu Chrome desde opencode (via extension + cxtd)

uso:
  cxt <op> ['<json-args>']
  cxt help

ops: ping | stat | tabs | tab.new | tab.activate | tab.close |
     navigate | back | forward | reload | read | click | type | key |
     scroll | wait | waitFor | eval | screenshot

ejemplos:
  cxt tabs
  cxt navigate '{"url":"https://www.facebook.com"}'
  cxt read '{"max":3000}'
  cxt click '{"text":"Publicar"}'
  cxt type '{"selector":"div[contenteditable=true]","value":"hola mundo"}'
  cxt eval '{"code":"document.title"}'
  cxt screenshot
`

func main() {
	args := os.Args[1:]
	if len(args) == 0 {
		fmt.Fprint(os.Stderr, usage)
		os.Exit(2)
	}
	op := args[0]
	if op == "help" || op == "-h" || op == "--help" {
		fmt.Print(usage)
		return
	}
	if op == "status" || op == "health" {
		server := os.Getenv("CXT_SERVER")
		if server == "" {
			server = "http://127.0.0.1:8799"
		}
		token := os.Getenv("CXT_TOKEN")
		endpoint := strings.TrimRight(server, "/") + "/health"
		if token != "" {
			endpoint += "?token=" + url.QueryEscape(token)
		}
		resp, err := http.Get(endpoint)
		if err != nil {
			fmt.Fprintf(os.Stderr, "error de conexion con cxtd: %v\n", err)
			os.Exit(1)
		}
		defer resp.Body.Close()
		rb, _ := io.ReadAll(resp.Body)
		fmt.Println(string(rb))
		return
	}

	raw := "{}"
	if len(args) > 1 {
		raw = strings.Join(args[1:], " ")
	}
	if !json.Valid([]byte(raw)) {
		b, _ := json.Marshal(map[string]string{"text": raw})
		raw = string(b)
	}

	server := os.Getenv("CXT_SERVER")
	if server == "" {
		server = "http://127.0.0.1:8799"
	}
	token := os.Getenv("CXT_TOKEN")
	endpoint := strings.TrimRight(server, "/") + "/cmd"
	if token != "" {
		endpoint += "?token=" + url.QueryEscape(token)
	}

	body, _ := json.Marshal(map[string]json.RawMessage{
		"op":   json.RawMessage(strconvQuote(op)),
		"args": json.RawMessage(raw),
	})
	client := &http.Client{Timeout: 70 * time.Second}
	resp, err := client.Post(endpoint, "application/json", bytes.NewReader(body))
	if err != nil {
		fmt.Fprintf(os.Stderr, "error de conexion con cxtd: %v\n", err)
		os.Exit(1)
	}
	defer resp.Body.Close()
	rb, _ := io.ReadAll(resp.Body)
	var rm resultMsg
	if err := json.Unmarshal(rb, &rm); err != nil {
		fmt.Fprintf(os.Stderr, "respuesta invalida (%d): %s\n", resp.StatusCode, string(rb))
		os.Exit(1)
	}
	if !rm.OK {
		fmt.Fprintf(os.Stderr, "error: %s\n", rm.Error)
		os.Exit(1)
	}
	printData(rm.Data)
}

func strconvQuote(s string) string {
	b, _ := json.Marshal(s)
	return string(b)
}

func printData(data json.RawMessage) {
	if len(data) == 0 {
		fmt.Println("{}")
		return
	}
	var v any
	if err := json.Unmarshal(data, &v); err != nil {
		fmt.Println(string(data))
		return
	}
	out, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		fmt.Println(string(data))
		return
	}
	fmt.Println(string(out))
}
