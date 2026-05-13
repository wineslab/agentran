# Contributing

Thanks for considering a contribution.

## Branch and PR conventions

- Cut feature branches off `release` (the long-lived release-prep branch).
- Open PRs against `release`. `main` is the public-facing branch and is
  updated when a batch of `release` work is ready to ship.
- One logical change per PR. Maintainers want to test each change
  independently — bundled refactors are hard to review and revert.
- If a change has to be split, stack the PRs: cut the second branch off the
  first, target both at `release`. GitHub recomputes the diff once the
  parent merges. Note the dependency in the PR description.

## Commit messages

Follow the existing repo style: an imperative subject (≤ 72 chars), then a
short paragraph explaining the *why*. Reference the issue or PR if
applicable.

## Pull request descriptions

PRs should include a short summary and a `## Test plan` section listing
what was actually verified. Distinguish lint / dry-run / static analysis
from real execution. If something couldn't be tested (hardware, paid API,
external service), say so explicitly — don't claim coverage you don't have.

## Issues

- Bugs and feature requests: open a GitHub issue with enough context to
  reproduce or evaluate.
- Broader design questions (e.g. multi-container topology, state-backend
  choices): also prefer issues — discussion threads on PRs get lost once
  the PR merges or closes.
- If you're not sure whether something is a bug or expected behavior, file
  it anyway; closing as wontfix is cheap.

## Adding a new agent

See [docs/CREATING_AGENTS.md](docs/CREATING_AGENTS.md).

## Code of conduct

Be kind. Assume good faith. If you wouldn't say it in a lab meeting, don't
say it in a review.
