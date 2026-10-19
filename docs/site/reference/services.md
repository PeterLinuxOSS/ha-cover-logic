# Services

Two services, the bridge between the YAML configuration format and the
subentries the UI edits. Both are also reachable from
**Settings → Devices & services → Cover Logic → Configure → Import / export
configuration**, which calls the same code.

## `cover_logic.import_config`

Reads a YAML file and replaces this integration's whole configuration with
it.

| Field | Required | Default | Meaning |
|---|---|---|---|
| `path` | yes | — | Path to the YAML file, e.g. `/config/cover_logic.yaml`. |
| `dry_run` | no | `false` | Validate and report what would change, without writing anything. |
| `overwrite` | no | `false` | Required to be `true` if the entry already has any configuration — see below. |

Returns a summary (visible in Developer Tools → Actions, "Response data"):
the counts of blinds, zones, modes, conditions, values and rules the file
contains, how many existing subentries would be/were replaced, and any
`validate()` warnings.

**Replace, never merge.** If the entry already has subentries configured and
`overwrite` is not `true`, the call is refused outright — every import
either replaces the whole configuration or changes nothing, so a result can
never be a mix of the file you just imported and whatever was there before.

**Validated before anything is written**, including under `dry_run`: the
file is parsed, checked for structural problems, and run through
`validate()` — so a dry run reports exactly the refusal a real run would
hit, never a false "this would succeed" followed by a real failure.

```yaml
# Developer Tools → Actions
action: cover_logic.import_config
data:
  path: /config/cover_logic.yaml
  dry_run: true
  overwrite: true
```

**Or have a model write the file for you.** The YAML dialect is documented for
exactly this: point an LLM at
[`MODELS.md` §4](https://github.com/PeterLinuxOSS/ha-cover-logic/blob/main/MODELS.md)
and a worked example,
[`docs/example-config.yaml`](https://github.com/PeterLinuxOSS/ha-cover-logic/blob/main/docs/example-config.yaml),
describe your house in plain language, and import the result with
`dry_run: true` first, as above.

## `cover_logic.export_config`

Writes the current configuration — whatever the subentries hold right now,
however they were built — out to a YAML file.

| Field | Required | Default | Meaning |
|---|---|---|---|
| `path` | yes | — | Path to write to, e.g. `/config/cover_logic_export.yaml`. |

**Refuses to write through a symlink** — if `path` is a symlink, the export
is refused rather than silently rewriting whatever the link points at.
Writing directly to a real, non-symlink path is always allowed, including
overwriting a file that already holds a configuration you are about to
replace.

**Refuses to overwrite a file that has comments.** `export_config` writes
from the parsed configuration, which carries no comments of its own — if the
target file already exists and contains at least one whole-line comment, the
export is refused rather than silently discarding them. Move the existing
file aside, or export to a different path, and merge any comments back by
hand.

## Errors

Every service call that fails raises with one of these keys. The message
shown always includes the specific detail (`{error_detail}`, `{path}`,
`{count}`) for that failure.

| Key | Meaning |
|---|---|
| `no_config_entry` | Cover Logic is not set up yet — add the integration first. |
| `invalid_config` | The file parsed, but `validate()` found at least one error-severity problem in it. |
| `cannot_parse_file` | The file is not valid YAML, or not valid in this configuration's dialect. |
| `cannot_read_file` | The path could not be opened at all (does not exist, no permission). |
| `existing_config` | `import_config` was called without `overwrite: true` while subentries already exist. |
| `import_conversion_failed` | The file passed `validate()`, but converting it into subentries failed internally — this is a bug in the integration, not in your file; please report it. |
| `export_path_is_symlink` | `export_config`'s `path` is a symlink; point it at the real file instead. |
| `export_path_is_directory` | `path` names a directory, not a file. |
| `export_parent_missing` | The directory `path` would be written into does not exist. |
| `export_target_has_comments` | `path` already exists and has comment lines that would be silently lost by exporting. |
| `nothing_to_export` | Nothing is configured yet, so there is nothing for `export_config` to write. |
| `cannot_parse_subentries` | The entry's current subentries could not be read back into a `Config` — an internal inconsistency; please report it. |
| `cannot_write_file` | The file could not be written (permissions, disk full, and similar). |
