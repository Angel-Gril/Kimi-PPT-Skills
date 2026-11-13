# Changelog

[简体中文](CHANGELOG.md) | [English](CHANGELOG_EN.md)

This project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Optional host-side export without a Kimi account or persistent browser profile; the default official exporter is unchanged.
- `export_pptx_host.py --auto-generate` renders the editor's latest saved `fileContent` at export time; `--pptx` keeps fixed-file semantics.
- `pptd_to_pptx.py` generates reference files directly, and `build_from_files()` renders in-memory YAML/JSON projects.
- Reference rendering preserves text, geometry, font sizes, theme colors, solid backgrounds, fade transitions and page order.

### Fixed

- Preserve the real editor's saved `rgb()/rgba()` text colors and opacity, preventing white text on dark backgrounds from turning black.
- Report the current rendered page count after pages are added or removed.
- Consolidate duplicate Unreleased entries and remove broken escape characters from file and function names.

### Security and validation

- Reuse project path boundaries, referenced-image filtering and media limits; validate PPTX relationships, XML, page counts and downloaded content.
- Pass file bytes through host RPC to support browser local-network restrictions; limit JSON render requests to the current host session.
- Record each render's digest and page count separately so concurrent requests cannot overwrite another download's validation state. Reject invalid file structures, absolute paths, `..` and duplicate paths.
- Preserve existing output unless `--force` is supplied, using output locks and transactional writes.
- Add regression coverage for real saved formats, page changes, request isolation, concurrent rendering and malformed inputs; run Node tests in CI.

### Limitations

- Mixed rich-text styling is simplified. Images, shapes, charts, element animations and font embedding are omitted; use `export_pptx.py` for the official writer.

## [1.2.0] - 2026-08-12

### Added

- Stable Playwright-based PPTX and page-image export, replacing `agent-browser` CLI sessions
- Dedicated Chromium profile with first-run sign-in and later reuse, fixing third-party storage partition authentication for the Kimi iframe under the localhost host
- Post-export counts for independent text, shapes, pictures, tables, and charts; image-only PPTX files are rejected by default
- Explicit diagnostics for signature HTTP 401, expired login, SDK state errors, and download timeouts

### Security

- Login and partition state remain in the dedicated profile for reuse; tokens are read and injected into the iframe partition only at runtime, and are never printed, written into PPTD or logs, or committed to the repository
- Automation never connects to the user's everyday browser, and failure URLs have query strings and fragments removed
- Added strict ignore rules and a release-time sensitive-data scan
- Added signed ownership markers for dedicated browser profiles, crash-safe export/install locks, OOXML relationship-graph validation, ZIP resource limits, and transactional output commits
- Page-image QA output must now live completely outside the PPTD project tree; the default is the sibling `<project-name>.qa-images` directory

### Fixed

- Fixed `/apiv2/utils/v1/signatures` returning HTTP 401 because Chrome partitioned storage hid top-level Kimi authentication from the embedded editor
- Fixed unreliable PPT Blob download capture through ordinary clicks or `agent-browser download`
- Fixed random CLI stalls caused by the editor's continuous network activity

## [1.1.1] - 2026-08-06

### Added

- Align PPTD with official element-level `animations`; Skill notes for animation / `notes` usage bounds
- Align image priority, anti-AI copy rules, clarification asks, replicate guidance, and parallel page writes
- Ship ~30 preset design systems, invoked only when named
- Restore `customFonts` (Google Fonts) and poster size recommendations
- Root theme catalogs: `theme.md` / `theme_EN.md` (with preview images)
- Sample project `example/xiaomi-yu7-ppt-animation` (on-slide entrance animations)

### Changed

- Sync scenario docs with official animation guidance and `customFonts` references
- README: document element animations, preset themes, and sample prompts

## [1.0.2] - 2026-08-06

### Changed

- `install` overwrites an existing skill by default; `--force` is no longer required (still accepted for compatibility)

## [1.0.1] - 2026-08-06

### Added

- Skill workflow **step0 prerequisite check**: verify Node.js 18+, npm/npx, and python3 before generation; note that a Chromium-based browser is required for export
- Export scripts check **Node.js 18+** and **npm** at startup, with clear install guidance when missing or too old
- CLI (`open-kimi-ppt-skills`) refuses to start when the Node.js major version is below 18
- Auto-install **PyYAML** via `pip install --user pyyaml` when missing (same pattern as Pillow / websocket-client)

### Docs

- Added multi-agent / multi-model example screenshots (ChatGPT·Codex + 5.6 Luna, Reasonix + DeepSeek, WorkBuddy, and more)
- Clarified install as “automatic or manual — pick one”, with Windows path notes
- Updated README structure and example images

## [1.0.0] - 2026-08-05

### Added

- Initial release of `open-kimi-ppt-skills`
- PPTD create / edit / replicate, delivering both an editable PPTD project and a PPTX by default
- Browser-side PPTX export (embedded fonts, fade transitions) with optional multimodal visual QA before export
- Local in-browser PPTD editor (`npx open-kimi-ppt-skills serve`)
- CLI to install the skill into `~/.agents/skills` (or another agent directory via `--target`)
