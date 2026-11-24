# Data model

SQLite `meta` records the schema version. `content` holds the stable identifier, title, body, summary, revision, timestamps, and visibility. A record has a type (`PROJECT`, `THOUGHT`, or `EXPERIENCE`), a primary space, optional related spaces, and a lifecycle (`DRAFT`, `PUBLISHED`, `COMPLETED`, or `ARCHIVED`). Lifecycle expresses the record's editorial state. Publication eligibility and deletion state are stored separately.

| Table | Purpose |
| --- | --- |
| `content_spaces` | Related space memberships for a content record |
| `relations` | Directed links for `PART_OF`, `DERIVED_FROM`, `LED_TO`, `EVOLVED_INTO`, and `REFERENCES` |
| `attributes` | Value, provenance, and confirmation attached to a record |
| `attachments` | Parent record, filename, byte size, digest, visibility, and integrity state |
| `operations`, `integrity_issues` | Attachment operation tracking and reconciliation |

Attachment bytes live in the archive's object directory. Release snapshots, history, and withdrawal state live in the local release registry. The public projection selects fields and uses separate public identifiers.

## Schema v1 to v2

`archive/db.py` applies registered migrations in order and commits each with its version update. The v2 migration adds the content model fields and related-space table. It preserves IDs, relationships, body text, and visibility, and extracts a summary only from a literal `Summary` section. Legacy rows receive the same default classification and remain editable after migration.
