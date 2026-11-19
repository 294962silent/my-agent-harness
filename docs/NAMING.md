# LongArc naming convention

## Canonical name

The product and project name is **LongArc**.

Use the following forms consistently:

- Display name: `LongArc`
- GitHub repository: `LongArc`
- Primary Python command: `longarc`
- Runtime descriptor: `long-horizon coding-agent runtime`
- Canonical repository URL: https://github.com/294962silent/LongArc

Do not use `LongArc Runtime` as the primary brand. “Agent runtime” may appear as
a descriptive subtitle.

## Compatibility

The rename does not remove existing local installations or data:

- `longarc` and `longarc-runtime` are the supported entry points.
- `my-pi-agent` and `my-agent` remain temporary command aliases.
- Internal Python package names such as `my_agent_core`, `my_agent_llm` and
  `my_coding_agent` remain unchanged to preserve imports.
- New local state defaults to `~/.longarc`.
- `~/.my-agent-harness` and `~/.my-pi-agent` remain supported fallback
  locations so existing auth, settings and sessions are not lost.
- `LONGARC_HOME` is the preferred explicit home override; `MY_AGENT_HOME`
  remains supported for existing installations.

## Historical evidence

Experiment reports, run IDs, source pins and archived output keep the project
name that was in effect when the evidence was produced. Historical names are
not rewritten because doing so would make old logs harder to verify.
