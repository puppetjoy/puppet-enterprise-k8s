# AGENTS

## Commit Messages

Use Linux kernel style commit messages for this repository.

- Write a short imperative subject line.
- Follow the subject with a blank line.
- Use a prose body that explains what changed and why.
- Wrap body text to a readable width.
- When scripting `git commit`, prefer `git commit -F <message-file>` with a
  here-doc or temp file so the rendered message is exactly what will be
  recorded.
- If you use `-m`, use one `-m` per paragraph and keep real newlines in the
  shell input; do not try to encode paragraph breaks with escape sequences.
- Do not put literal `\n` escape sequences into commit text expecting Git to turn them into newlines.
- After any scripted `commit`, `amend`, or history rewrite, check the rendered
  message with `git log -1 --format=%B` or an equivalent `git log` view before
  pushing.
