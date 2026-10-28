"""Security controls for SilverFile. docs/THREAT_MODEL.md maps each module to
the threats it answers and the tests that prove it:

  pii         SSN validation (900-series only) and redaction for logs and traces
  crypto      encryption at rest (checkpoints, uploads, the index) and the HMAC key
  uploads     file-type allowlist, magic bytes, size limits, EXIF stripping, encrypted storage
  validate    schema checks on extracted values, HITL fact patches and checkpointed state
  injection   instruction-like text in answers and documents
  integrity   the hash-chained seal over the provenance of a reviewed draft
  audit       the tamper-evident security event log
  auth        staff accounts, roles, sessions, taxpayer access codes
  ratelimit   request rate limits and the per-return model budget
  retention   deletion of abandoned returns
  capabilities which tax_math tools each agent may call
"""
