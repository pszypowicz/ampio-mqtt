# ampio-mqtt project instructions

## Markdown

- Do not hand-manage line breaks in `.md` files. Prettier owns the wrapping:
  `.prettierrc` sets `proseWrap: always` (80 columns), and tables and code
  blocks stay as long as they need to be. Run
  `git ls-files '*.md' | xargs npx prettier@3.6.2 --write` after edits, or let
  the pre-commit hook do it. The tracked set is the subject, so scratch files
  you keep in the clone are left alone. CI rejects unwrapped prose.
- Write documentation prose in Simplified Technical English: short sentences (25
  words maximum), no semicolons, the modals can/will/must only, conditions
  before commands, one word per concept.
- Reader-facing docs never reference GitHub issues. State each caveat in place.
  `docs/untapped-surfaces.md` is the one page that links to the tracker, and
  every entry there must read alone.

## Checks

- `uv run pytest -q`, `uv run ruff check src tests tools`,
  `uv run ruff format --check src tests tools`, and
  `uv run mypy src/ampio_mqtt tools` must pass before a commit. CI runs all
  four. `uvx pre-commit install` wires them locally.
