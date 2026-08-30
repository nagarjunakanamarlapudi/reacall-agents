"""Regenerate the deterministic Northstar digital twin."""

from recallops.data.generator import generate_demo_dataset

if __name__ == "__main__":
    generated = generate_demo_dataset()
    print(generated["manifest"]["sha256"])
