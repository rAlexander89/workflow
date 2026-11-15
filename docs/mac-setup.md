# Setup for Apple Silicon Macs (Claude Code flow)

This is the first flow: an operator-provisioned user runs the workflow on their own
M-series Mac, under their own Claude Code subscription login.

## 1. Install the prerequisites

- **Python 3.11 or newer.** Homebrew: `brew install python@3.14`
- **git.** Included with Xcode Command Line Tools (`xcode-select --install`).
- **Claude Code.** Install it using Anthropic's current Claude Code install instructions. Confirm `which claude` prints a path afterwards.

## 2. Sign in with your subscription, not an API key

```bash
claude auth login          # choose the Claude subscription option
claude auth status --text  # expect a signed-in account, not an API key
```

If `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` is set in your shell, remove it from
your shell profile. Runs remove these variables themselves, but a leftover key in an
interactive shell is the usual reason a login ends up on API billing.

## 3. Install the workflow

```bash
cd wf
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## 4. Enable access on this machine (operator-provisioned users only)

```bash
wf model set --provider claude-code --model <model-name> --enable-claude-code
```

## 5. Check the machine

```bash
wf doctor
```

Every line should be `ok`, or `warn` for the key-variable line. Fix any `FAIL` line
before starting a run. The check text names the fix.

## Notes

- Each run is a child process with a time limit. No `timeout` command is needed.
- Runs stay on this Mac. Nothing is copied to another machine.
- The run container and the Linux runtime in `docs/plan.md` are not part of this flow yet.
