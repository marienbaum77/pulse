# Contributing to Pulse

We welcome contributions! Whether it's a bug report, feature request, or pull
request — every contribution helps.

## Reporting issues

- **Bug reports**: use the [Bug report template](.github/ISSUE_TEMPLATE/bug_report.md)
- **Feature requests**: use the [Feature request template](.github/ISSUE_TEMPLATE/feature_request.md)
- **Security vulnerabilities**: see [SECURITY.md](SECURITY.md)

## Pull requests

1. Fork and create a branch from `main`
2. Make your changes
3. Write or update tests if needed
4. Run the test suite — see [docs/development.md](docs/development.md)
5. Follow the [conventional commits](#conventional-commits) convention
6. Submit a pull request

## Conventional commits

Commit messages follow the Conventional Commits specification:

```
<type>[optional scope]: <description>

[optional body]

[optional footer(s)]
```

**Types:** `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`

**Examples:**

```
feat(api): add manual schedule publish mode
fix(pipeline): restore import after router split
docs: update deployment guide
chore: bump electron to 33.0.0
```

## Code style

- **Python**: keep it simple. No linter/formatter is required, but use PEP 8
  conventions and `ruff` if you have it.
- **TypeScript/JSX**: use the project's `tsc` type-check. ESLint is optional.
- **YAML**: 2-space indentation, LF line endings.

## Committing

Run `git commit -m "feat: your message"` — no merge commits. Squash your
branch into logical commits before submitting.
