"""AI voice notes: a caregiver's spoken visit note turned into an editable transcript and summary.

The design, the contract and the reasons are in docs/plans/2026-11-07-ai-note-transcription.md; what shipped and where it differs from
the plan is in docs/sop/2026-11-08-ai-note-transcription.md. The rule that governs every module here: audio and text are never logged,
never put in an audit row and never kept outside the encrypted columns (and the audio only in Redis, encrypted, until the text exists).
"""
