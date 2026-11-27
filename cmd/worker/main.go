// Command worker consumes autofix runs from the queue and executes the fix
// flow: normalize review feedback, run the omp CLI agent, apply patches,
// validate, commit, push and write back to the forge.
//
// Milestone status: M0 scaffold — the queue drain loop lands in M3.
package main

import (
	"log"

	"github.com/sun-praise/software-factory/internal/db"
)

func main() {
	if _, err := db.Open(); err != nil {
		log.Fatalf("open state store: %v", err)
	}
	log.Print("worker scaffold up; queue drain arrives with M3")
}
