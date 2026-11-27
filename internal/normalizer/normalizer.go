// Package normalizer converts raw forge review/comment events into
// structured autofix work items via an LLM agent built on trpc-agent-go.
//
// Milestone status: M0 declares the data contract (ported from
// app/schemas/normalizer.py and docs/architecture.md); the agent pipeline
// lands in M2.
package normalizer

import "trpc.group/trpc-go/trpc-agent-go/model"

// CommentFixItem is one actionable comment inside a NormalizedReview.
type CommentFixItem struct {
	Source   string `json:"source"`
	Path     string `json:"path,omitempty"`
	Line     int    `json:"line,omitempty"`
	Severity string `json:"severity,omitempty"`
	Text     string `json:"text"`
}

// NormalizedReview mirrors the normalized review JSON documented in
// docs/architecture.md.
type NormalizedReview struct {
	Repo      string           `json:"repo"`
	PRNumber  int              `json:"pr_number"`
	HeadSHA   string           `json:"head_sha,omitempty"`
	MustFix   []CommentFixItem `json:"must_fix"`
	ShouldFix []CommentFixItem `json:"should_fix"`
	Ignore    []CommentFixItem `json:"ignore"`
	Summary   string           `json:"summary"`
}

// Normalizer turns raw forge events into NormalizedReview values.
type Normalizer struct {
	// Model is the LLM backend (anthropic/openai compatible) used by the
	// normalization agent once the M2 pipeline is wired in.
	Model model.Model
}
