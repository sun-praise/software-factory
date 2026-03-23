## ADDED Requirements

### Requirement: Standalone issue intake SHALL produce actionable run context
The system SHALL accept a standalone GitHub issue as an autofix source and
resolve its actionable context before the agent starts execution.

#### Scenario: Queue a standalone issue run
- **WHEN** an operator submits a GitHub issue URL that is not an existing pull request
- **THEN** the system stores the issue identity separately from any pull request identity
- **AND** the queued run contains structured issue context derived from the issue body, comments, or explicit operator input

### Requirement: Issue-backed runs SHALL create a dedicated working branch
The system SHALL create a new working branch for an issue-backed run from a
known base branch instead of requiring an existing pull request head branch.

#### Scenario: Create branch for issue-backed implementation
- **WHEN** an issue-backed run starts execution
- **THEN** the runner checks out the configured base branch for the repository
- **AND** creates a dedicated working branch for that issue before the agent edits code

### Requirement: Successful issue-backed runs SHALL open a new pull request
The system SHALL create a new pull request after it successfully pushes code for
an issue-backed run.

#### Scenario: Open pull request after push
- **WHEN** an issue-backed run completes code changes and pushes its working branch successfully
- **THEN** the system creates a pull request targeting the selected base branch
- **AND** records the created pull request number and URL in the run state

### Requirement: Issue-backed runs SHALL report completion back to the source issue
The system SHALL report the outcome of an issue-backed run back to the original
GitHub issue.

#### Scenario: Comment on source issue after PR creation
- **WHEN** the system creates a pull request for an issue-backed run
- **THEN** it posts a comment on the source issue with the created pull request URL and run summary

### Requirement: Issue-backed runs SHALL fail with explicit operator-visible errors
The system SHALL stop issue-backed runs with explicit errors when required issue
context, branch creation, push, or pull request creation cannot complete.

#### Scenario: Fail before agent execution when issue context is missing
- **WHEN** the submitted issue does not provide actionable context and no explicit operator note is available
- **THEN** the system rejects or fails the run with a reason that tells the operator what additional context is required
