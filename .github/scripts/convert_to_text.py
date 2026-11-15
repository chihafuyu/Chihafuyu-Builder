"""
Convert checkbox-based patcher workflows to text-based input.
Also normalizes app descriptions to match their slugified input keys.
Run manually via GitHub Actions workflow_dispatch.
"""

import os
import re
import sys

MAX_YAML_INPUTS = 14
APP_INPUT_RE = re.compile(r"^ {6}app_[a-zA-Z0-9_]+:\r?\n", re.MULTILINE)


def _get_repo_root() -> str:
    """Returns the absolute path to the repository root."""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def normalize_descriptions(content: str) -> str:
    """Normalizes app descriptions to match their slugified input keys."""
    pattern = re.compile(
        r"^( {6})app_([a-zA-Z0-9_]+):(\r?\n)"
        r"([ \t]+description:\s*['\"]?Patch [^'\"\r\n]+['\"]?)(\r?\n)",
        re.MULTILINE
    )

    def repl(match):
        indent = match.group(1)
        app_key = match.group(2)
        newline = match.group(3)
        desc_part = match.group(4)
        desc_indent = re.match(r"^([ \t]+)", desc_part).group(1)
        # Convert app_key (e.g., youtube_music) to slug (youtube-music)
        slug = app_key.replace("_", "-").lower()
        return (
            f"{indent}app_{app_key}:{newline}"
            f"{desc_indent}description: 'Patch {slug}'{newline}"
        )

    return pattern.sub(repl, content)


def _build_text_yaml(ecosystem_name: str, call_patcher_block: str, workflow_name: str) -> str:
    """Generates the text-based YAML structure."""
    return f"""---
name: {workflow_name}

"on":
  workflow_dispatch:
    inputs:
      apps_to_patch:
        description: >-
          Type "all" to follow config/apps-{ecosystem_name}.toml,
          or type the name of a specific app (eg: 1weather,3way)
        required: true
        type: string
        default: 'all'
      architecture:
        description: 'Target Architecture'
        required: true
        type: choice
        default: 'arm64-v8a'
        options: [arm64-v8a, armeabi-v7a, x86_64, x86, universal]
      version_selection:
        description: 'App Version Selection'
        required: true
        type: choice
        default: 'Latest Recommended'
        options: [Latest Recommended, Beta, Custom]
      custom_version:
        description: 'Custom App Version'
        required: false
        type: string
        default: ''
      download_source:
        description: 'Download Source'
        required: true
        type: choice
        default: 'default'
        options:
          - default
          - google_play
          - huggingface
          - archive
          - apkmirror
          - apkpure
      continue_on_error:
        description: 'Debug: Continue on patch error'
        required: true
        type: boolean
        default: false
      cli_track:
        description: 'Morphe CLI Track'
        required: true
        type: choice
        default: 'Stable'
        options: [Stable, Pre-release, Custom]
      custom_cli_version:
        description: 'Custom CLI Version (e.g. v1.16.0)'
        required: false
        type: string
        default: ''
      patches_track:
        description: 'Ecosystem Patches Track'
        required: true
        type: choice
        default: 'Stable'
        options: [Stable, Pre-release, Custom Local]
      upload_github_release:
        description: 'Upload to GitHub Release'
        required: true
        type: boolean
        default: true
      upload_huggingface:
        description: 'Upload to HuggingFace Dataset'
        required: false
        type: boolean
        default: false
      send_to_telegram:
        description: 'Send to Telegram'
        required: true
        type: boolean
        default: false

permissions: {{}}

jobs:
  prepare-apps:
    runs-on: ubuntu-latest
    outputs:
      selected: ${{{{ steps.build-list.outputs.apps }}}}
    steps:
      - name: Checkout Repository
        uses: actions/checkout@v7

      - name: Set up Python
        uses: actions/setup-python@v7.1.0
        with:
          python-version: '3.14'

      - name: Validate and Normalize App List
        id: build-list
        env:
          APPS_RAW: ${{{{ inputs.apps_to_patch }}}}
          ECO_NAME: "{ecosystem_name}"
        run: |
          set -euo pipefail

          ECO="$ECO_NAME"
          CONFIG="ecosystem/${{ECO}}.json"
          TOML_CONFIG="config/apps-${{ECO}}.toml"

          if [ ! -f "$CONFIG" ]; then
            echo "::error::Config not found: $CONFIG"
            exit 1
          fi

          SELECTED=$(CONFIG_PATH="$CONFIG" TOML_PATH="$TOML_CONFIG" ECO_NAME="$ECO" APPS_RAW="$APPS_RAW" python3 -c "
          import os, json, sys

          try:
              import tomllib
          except ImportError:
              print('::error::Python 3.11+ required for tomllib', file=sys.stderr)
              sys.exit(1)

          eco = os.environ['ECO_NAME']
          config_path = os.environ['CONFIG_PATH']
          toml_path = os.environ['TOML_PATH']
          raw_input = os.environ.get('APPS_RAW', '').strip()

          with open(config_path) as f:
              valid_apps = json.load(f)[eco]['apps']

          selected = []

          if raw_input.lower() == 'all':
              print(f'[INFO] Mode: Reading from {{toml_path}}', file=sys.stderr)
              if not os.path.exists(toml_path):
                  print(f'::error::TOML file not found: {{toml_path}}', file=sys.stderr)
                  sys.exit(1)

              with open(toml_path, 'rb') as f:
                  toml_data = tomllib.load(f)

              apps_dict = toml_data.get('apps', {{}})
              selected = [k for k, v in apps_dict.items() if v is True]

              if not selected:
                  print(f'::error::No apps enabled in {{toml_path}}', file=sys.stderr)
                  sys.exit(1)

          else:
              print('[INFO] Mode: Manual Input', file=sys.stderr)
              selected = [a.strip() for a in raw_input.split(',') if a.strip()]

          invalid = [app for app in selected if app not in valid_apps]
          if invalid:
              print(f'::error::The following apps were not found in {{config_path}}: {{invalid}}', file=sys.stderr)
              print('Valid apps:', file=sys.stderr)
              for v in sorted(valid_apps):
                  print(f'  - {{v}}', file=sys.stderr)
              sys.exit(1)

          print(','.join(selected))
          ")

          if [ -z "$SELECTED" ]; then
            echo "::error::No apps selected."
            exit 1
          fi

          echo "Apps to patch: $SELECTED"

          {{
            echo "apps<<EOF"
            echo "$SELECTED"
            echo "EOF"
          }} >> "$GITHUB_OUTPUT"

{call_patcher_block}"""


def convert_workflow(yaml_path: str) -> bool:
    """Converts a single YAML file if it exceeds the input limit and normalizes descriptions."""
    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            original_content = f.read()
    except OSError as err:
        print(f"Failed to read {yaml_path}: {err}")
        return False

    if "apps_to_patch:" in original_content:
        return False

    # 1. Normalize descriptions
    content = normalize_descriptions(original_content)

    # 2. Check for text-based migration
    app_inputs = len(APP_INPUT_RE.findall(content))
    if app_inputs > MAX_YAML_INPUTS:
        name_match = re.search(r"^name:\s*(.*)$", content, re.MULTILINE)
        workflow_name = name_match.group(1) if name_match else "Ecosystem Patcher"

        call_patcher_match = re.search(r"^  call-patcher:", content, re.MULTILINE)
        if not call_patcher_match:
            print(f"Skipping {yaml_path}: could not find 'call-patcher:' block.")
            return False

        call_patcher_block = content[call_patcher_match.start():]
        ecosystem_name = os.path.basename(yaml_path).replace("-patcher.yml", "")

        content = _build_text_yaml(ecosystem_name, call_patcher_block, workflow_name)
        print(f"Migrating {yaml_path} to text-based input.")

    if content == original_content:
        return False

    tmp_path = f"{yaml_path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, yaml_path)
        print(f"Successfully updated {yaml_path}.")
        return True
    except OSError as err:
        print(f"Failed to write {yaml_path}: {err}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return False


def main() -> None:
    """Scan workflows directory and convert overflowing YAMLs."""
    workflows_dir = os.path.join(_get_repo_root(), ".github", "workflows")
    if not os.path.isdir(workflows_dir):
        print("Workflows directory not found.")
        sys.exit(1)

    changed = False
    for filename in os.listdir(workflows_dir):
        if filename.endswith("-patcher.yml"):
            yaml_path = os.path.join(workflows_dir, filename)
            print(f"Checking {filename}...")
            if convert_workflow(yaml_path):
                changed = True

    if not changed:
        print("No workflows required updates.")
    else:
        print("Migration complete.")


if __name__ == "__main__":
    main()
