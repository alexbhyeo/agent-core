# A2UI Browser Login Resume

## Metadata

| Item | Value |
| --- | --- |
| Date | 2026-10-02 |
| Scope | A2UI login submission routing, resumable flow lifecycle, browser status summaries |
| Specs | S_05 |
| Baseline | Mocked WebSocket and browser-agent tests; status-logger unit tests |
| Refs | Testing branch; no issue assigned |

## Background

The login form previously returned credentials through the outer model's tool
call. The client also ignores submissions while chat processing is active, so
forms delivered before completion could appear unresponsive. Tool rails could
rewrite arguments to mappings retained in history, violating the JSON-string
format required by OpenAI-compatible APIs.

## State and Decisions

Store a random flow ID with the paused inner session, interrupt ID, conversation,
expected field keys, expiry, and busy flag. The WebSocket intercepts credential
submissions before producing an outer-model message, checks the flow through a
direct backend resume, and clears its temporary credential mapping afterwards.
Successful execution consumes the flow. Failures and cancellation release the
busy flag; expiry and conversation/field mismatches reject the submission.

Send deferred login forms after completion for both initial and resumed runs.
Keep batch/fill-form target information in browser status logs, replacing values
with redacted metadata. Normalize rail-rewritten tool arguments and assistant
history serialization through one JSON-string helper.

## Rejected Alternatives

Do not ask the outer model to remember and replay the login token or credentials.
Do not restart the browser task when a form is submitted. Do not log raw field
values to diagnose failed browser actions.

## Verification

Unit tests cover form creation, model-facing schema, direct submission routing,
conversation binding, retry after failure/cancellation, and form ordering after
both initial execution and resume. Status tests check that target information is
retained without submitted values. A serialization regression reproduces a
mapping assigned to an already validated tool call.

## Known Limits

The generic A2UI form remains a temporary compatibility interface; a native
masked form and dedicated credential envelope remain follow-up work. Credentials
still reach the inner agent via `InteractiveInput`. Pending flows live in process
memory, and restarts lose them. These tests do not exercise a live account or
prove that all model/runtime traces redact secrets.
