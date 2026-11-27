// Command web runs the HTTP orchestrator: hook events, forge webhooks and
// the thin SSR web UI. It must never receive AI provider tokens; the worker
// owns those (see docs/local-runtime.md).
package main

import (
	"log"
	"net/http"

	"github.com/sun-praise/software-factory/internal/config"
	"github.com/sun-praise/software-factory/internal/db"
)

func main() {
	if _, err := db.Open(); err != nil {
		log.Fatalf("open state store: %v", err)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("ok\n"))
	})

	addr := ":" + config.WebPort()
	log.Printf("software-factory web listening on %s", addr)
	log.Fatal(http.ListenAndServe(addr, mux))
}
