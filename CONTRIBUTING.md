# Contributing to EzClassify Python SDK

Thanks for your interest in contributing. The short version: sign off your
commits, fork, open an issue before anything non-trivial, and bring tests.
Environment setup and build mechanics live in
[DEVELOPMENT.md](./DEVELOPMENT.md): the repo layout, editable-install and test
commands, the regen and spec-vendoring workflow, and running conformance.

## Before You Start

1. **Sign off your commits.** Contributions come in under the
   [Developer Certificate of Origin 1.1](https://developercertificate.org/).
   Signing off is how you state that you wrote the patch, or otherwise have
   the right to submit it under this project's license. Add the line with
   `git commit -s`, which appends:

   ```
   Signed-off-by: Your Name <you@example.com>
   ```

   Use your real name and an address you can be reached at. Every commit in a
   pull request needs the line.

   *Counsel reviews the contribution terms. This engineering guide describes
   the DCO process and is not legal advice.*
2. **Fork the repository.** All contributions come through pull requests from
   forks.
3. **Open an issue first** for non-trivial changes, so we can agree on the
   approach before you invest the work.

## Pull Requests

- **Bring tests.** New behavior needs tests; bug fixes need a regression test
  that fails without the fix. The conformance and unit suites are the functional
  gate; CI runs them.
- **Follow the engineering standards** in [STANDARDS.md](STANDARDS.md) for
  design, code quality, and testing. Read it before significant work.
- Keep pull requests focused; a reviewer should be able to hold the whole change
  in their head.

## Commit Messages

Conventional commits (`fix:`, `feat:`, `docs:`).

## Reporting Security Issues

Report vulnerabilities privately per the [security policy](https://github.com/ezclassify/ezclassify/blob/main/SECURITY.md), never in a
public issue.

## Code of Conduct

This project follows our [Code of Conduct](https://github.com/ezclassify/ezclassify/blob/main/CODE_OF_CONDUCT.md). By
participating, you are expected to uphold it.
