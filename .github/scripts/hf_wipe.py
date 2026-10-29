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

        # Keep repository metadata files
        excluded_files = {"README.md", ".gitattributes"}
        files_to_delete = [f for f in files if f not in excluded_files]

        if not files_to_delete:
            print("[WARN] No files to delete after exclusions. Skipping.")
            return

        print(f"[INFO] Found {len(files_to_delete)} files to delete. Proceeding...")

        # Delete specific files
        api.delete_files(
            repo_id=hf_dataset,
            delete_patterns=files_to_delete,
            repo_type="dataset",
            commit_message="chore: wipe out entire dataset via automated workflow"
        )
        print("[SUCCESS] Successfully wiped out all files from the dataset.")
    except Exception as err:  # pylint: disable=broad-exception-caught
        print(f"[FATAL] Failed to wipe dataset: {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
