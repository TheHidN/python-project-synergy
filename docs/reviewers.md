# Reviewers that never interrupt you

`synergy review prepare NAME` builds a ready-made review folder under the state folder:

- `code/`: a frozen, read-only copy of the committed code (`git archive`), so the reviewer reads the real files;
- `kit/`: two commands that run inside a bubblewrap sandbox: `run-tests.sh` and `run-probe.sh`;
- `work/`: the only writable folder; `BRIEF.md`: what to do; `settings.json`; `launch.sh`.

The launcher starts one reviewer with `--permission-mode dontAsk`: a command that is not on the short allow list is
refused instead of waiting for you. The allow list is the two kit commands and writing inside `work/` and
`REVIEW.md`. The settings also deny reading the registry, the token folders and your Claude profile data.

## What the sandbox gives you

Inside it there is no network, your home folder is empty, the system is read-only, sockets under `/run` are hidden,
the environment is scrubbed and only `work/` is writable. This is enforced by the operating system, not by matching
command text. The kit refuses to run when bubblewrap is missing.

## Limits, stated plainly

- The reviewer's own file-reading tools run outside the sandbox; the deny rules for the registry and tokens are
  permission rules, not an operating-system wall. A reviewer is a cooperative helper, not a hostile program.
- The `dontAsk` mode and these settings follow the documentation; try one disposable review before relying on it.
- Bypass mode is your choice for operator sessions. It is not used for reviewers, and no mode guarantees safety.
