"""HuggingFace Dataset Wiper script."""

import os
import sys

from huggingface_hub import HfApi


def main() -> None:
    """Wipe all files and folders from a HuggingFace dataset."""
    hf_token = os.environ.get("HF_TOKEN", "")
    hf_dataset = os.environ.get("HF_DATASET_ID", "")

    if not hf_token or not hf_dataset:
        print("[FATAL] HF_TOKEN and HF_DATASET_ID must be provided.")
        sys.exit(1)

    print(f"[INFO] Initializing wipe for HuggingFace Dataset: {hf_dataset}")
    api = HfApi(token=hf_token)

    try:
        files = api.list_repo_files(repo_id=hf_dataset, repo_type="dataset")
        if not files:
            print(f"[WARN] Dataset {hf_dataset} is already empty. Nothing to wipe.")
            return

        print(f"[INFO] Found {len(files)} files. Proceeding with deletion...")

        # Delete all files and folders recursively
        api.delete_files(
            repo_id=hf_dataset,
            delete_patterns=["*"],
            repo_type="dataset",
            commit_message="chore: wipe out entire dataset via automated workflow"
        )
        print("[SUCCESS] Successfully wiped out all files from the dataset.")
    except Exception as err:
        print(f"[FATAL] Failed to wipe dataset: {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
