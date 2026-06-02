# Codex Assistant Repository Instructions

- Keep the Telegram bot runtime stdlib-only unless a dependency is explicitly justified.
- Do not commit local Codex auth, Telegram secrets, project index, task state, logs, or sqlite files.
- Keep Docker/install changes portable across Linux user homes.
- Run `python -m unittest discover -s tests` after code changes.
- For Docker changes, run `docker compose config` and build the image when practical.
