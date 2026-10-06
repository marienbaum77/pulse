# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.1] - 2026-10-07

### Added

- Topic scoring by aspect (clustering quality, source authority, freshness)
- Cover image filtering for generated posts
- Ingest slot management to keep workers free
- Troubleshooting notes for provider 403 errors and Telegram cover images

### Changed

- Topic fit evaluation uses cluster headlines instead of individual articles
- Improved LLM error resilience with transient-error retry
- Long Telegram posts are sent as one message again, with the cover shown as a link preview of the source article

### Fixed

- API helper imports after router split
- Topic threshold default migration
- Topic threshold for existing installations
- Cover image download is retried on temporary failures; a post is no longer published as text-only when its cover could not be loaded
