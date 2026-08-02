# Application Studio Architecture

Tech CV treats a job application as a connected, evidence-backed workflow. A
job status, generated document, or browser-open event is not interchangeable
with an external submission.

## Product graph

```text
Candidate facts and source CV
              |
              v
Job opportunity -> reviewed change proposal -> application packet
                                                |          |
                                                |          +-> match snapshot
                                                +-> tailored CV revision
                                                +-> cover-letter revision
                                                +-> verified PDF artifacts
                                                +-> readiness confirmations
                                                +-> append-only apply attempts
```

Every surface preserves the same identifiers:

- `job_id`: the opportunity and captured job description.
- `source_cv_id`: the user-selected source document.
- `tailored_cv_id`: the reviewed job-specific CV.
- `cover_letter_id`: the reviewed job-specific letter.
- `packet_id`: the versioned manifest tying those inputs and outputs together.

## Surface ownership

### Jobs / Application Studio

Jobs owns the complete workflow. It selects the source CV, starts review,
creates the CV and letter, confirms the current letter hash, builds artifacts,
shows readiness blockers, records explicit confirmations, and prepares a safe
manual-open action. It never claims that opening a target submitted anything.

### CV editor

The editor owns structured document authoring and one-page preview/export
parity. Focus mode edits one section without mounting a second source of truth;
Overview retains full-document scanning and reordering. A linked CV exposes its
job and packet context instead of becoming an anonymous document copy.

### Tailor

Tailor is a focused entry into the same application aggregate. It may create a
reviewed tailored CV, but the next action returns to cover-letter and packet
completion. It cannot create a disconnected terminal artifact.

### Cover Letters

Cover Letters owns full letter authoring: recipient, company, role, tone,
subject, salutation, body, closing, and attachments. It preserves job and CV
context and returns to packet readiness.

## State semantics

| State | Evidence |
| --- | --- |
| Saved job | Owned `saved_jobs` row |
| Review proposed | Versioned review token plus source/job hashes |
| Packet created | Reviewed CV and letter IDs plus current content hashes |
| Ready | Current packet, required PDFs, source/job/document hashes, review confirmation, profile/legal/visa confirmations, and safe target |
| Manual open ready | Append-only attempt with `submitted=false` and `automation_performed=false` |

New catalog mutations reject `saved_jobs.status = applied`. Historical rows
with that value are displayed as an unverified legacy note, never as evidence.
There is no packet-level submission state. Manual-open evidence records only
that a validated target link was prepared after explicit confirmations. The
application cannot observe whether the user clicked, opened, or submitted it.

## Transaction boundaries

- Imported-result linking and saved-job creation are one idempotent transaction.
- Star and source-revalidation writes lock and merge the current metadata row.
- Packet replacement, partial lineage merge, cover-review confirmation, and
  final apply-action evidence revalidate current job/document hashes under a
  deterministic profile -> job -> packet -> document lock order.
- Account deletion detaches external artifacts atomically; generation-scoped
  object keys and a token-bound outbox prevent delayed cleanup from deleting a
  replacement PDF.
- Release cutover runs a read-only schema descriptor, exact RPC-signature and
  relation-shape checks, worker-version evidence, and authenticated-grant
  attestation before any candidate service starts. An incompatible rollback
  remains in maintenance instead of exposing an unverified old or new stack.

## Rendering contract

- CV output remains exactly one A4 page or the build fails.
- Cover letters retain their independent flowing-letter renderer and may span
  multiple pages; readiness requires a real current PDF artifact, not a one-page
  cover-letter fiction.
- Preview and export use the same persisted document content and appearance
  inputs.
- Invalid or stale generated work never replaces the last valid artifact.

## Truthfulness contract

- Job requirements are target evidence, not candidate facts.
- Candidate claims must originate in the source CV or a separately verified
  candidate fact.
- Unsupported requirements appear as gaps.
- Generated phrasing remains reviewable before it becomes a document revision.
- Packet hashes make post-review source, job, CV, and letter drift visible.

## Destructive operations

Authenticated clients cannot directly delete saved jobs, documents, or mutate
PDF artifact metadata. Guarded deletion RPCs lock the owned parent, reject any
application-history relationship, atomically preserve external-object cleanup
evidence, and only then delete. Failed R2 cleanup remains in a durable retry
queue. Account erasure remains a separate service-role workflow.

## Next domain migration

The current YAML document remains the stable renderer projection. The next
storage evolution should add normalized entities without breaking it:

```text
CandidateProfile
  -> CandidateFact[]
       -> evidence, confidence, verification state

JobSnapshot
  -> Requirement[]
       -> importance, source span, extraction version

Application
  -> PacketVersion[]
  -> ChangeProposal[]
  -> StatusEvent[]
  -> Task[]
```

A compiler can then select verified facts and ordered content blocks into the
existing YAML shape. That prevents job-specific CV copies from becoming
divergent sources of personal truth while preserving the renderer and one-page
gate.

## Reference boundaries

Reactive Resume and Resume Matcher inform product patterns only. No external
source is copied. Useful patterns are content/layout separation, queued
autosave, durable revision checkpoints, same-pipeline preview/export,
requirement-to-evidence analysis, allowlisted structured changes, and explicit
diff review. Their shallow or local-only job persistence, unversioned scores,
ephemeral letters, and unsupported-skill inference are not adopted.
