"""HuggingFace Dataset Uploader script."""

import os
import sys
from pathlib import Path

from huggingface_hub import HfApi


def main() -> None:
    """Upload patched APKs from the output directory to HuggingFace."""
    ecosystem = os.environ.get("ECOSYSTEM", "")
    hf_token = os.environ.get("HF_TOKEN", "")
    hf_dataset = os.environ.get("HF_DATASET_ID", "")

    if not ecosystem:
        print("[FATAL] ECOSYSTEM environment variable is required.")
        sys.exit(1)

    if not hf_token or not hf_dataset:
        print("[FATAL] HF_TOKEN and HF_DATASET_ID must be provided.")
        sys.exit(1)

    output_dir = Path(f"{ecosystem}/Output")
    if not output_dir.is_dir():
        print(f"[FATAL] Output directory not found: {output_dir}")
        sys.exit(1)

    apk_files = sorted(output_dir.glob("*.apk"))
    if not apk_files:
        print(f"[WARN] No APK files found in {output_dir}. Skipping upload.")
        return

    print(f"[INFO] Preparing {len(apk_files)} file(s) for HuggingFace Dataset: {hf_dataset}")

    api = HfApi(token=hf_token)

    for apk in apk_files:
        print(f"[INFO] Uploading {apk.name} to {ecosystem}/ folder...")
        try:
            api.upload_file(
                path_or_fileobj=str(apk),
                path_in_repo=f"{ecosystem}/{apk.name}",
                repo_id=hf_dataset,
                repo_type="dataset",
                commit_message=f"chore: upload {apk.name} from {ecosystem}"
            )
            print(f"[SUCCESS] Uploaded {apk.name}")
        except Exception as err:
            print(f"[ERROR] Failed to upload {apk.name}: {err}")

    print("[INFO] HuggingFace upload sequence complete!")


if __name__ == "__main__":
    main()
