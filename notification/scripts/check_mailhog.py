"""Count the emails in MailHog per recipient and flag any duplicates.

    python -m scripts.check_mailhog            # all recipients
    python -m scripts.check_mailhog --clear    # empty the inbox first (before a demo)

Exits with status 1 if any recipient got the same email (same
X-Notification-Key) more than once.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

import httpx


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("MAILHOG_API", "http://mailhog:8025"))
    parser.add_argument("--clear", action="store_true")
    args = parser.parse_args()

    if args.clear:
        httpx.delete(f"{args.url}/api/v1/messages").raise_for_status()
        print("MailHog inbox cleared")
        return

    data = httpx.get(f"{args.url}/api/v2/messages", params={"limit": 5000}).raise_for_status().json()
    per_key: Counter[tuple[str, str]] = Counter()
    for item in data.get("items", []):
        headers = item["Content"]["Headers"]
        to = headers.get("To", ["?"])[0]
        key = headers.get("X-Notification-Key", ["?"])[0]
        per_key[(to, key)] += 1

    dupes = {k: n for k, n in per_key.items() if n > 1}
    for (to, key), n in sorted(per_key.items()):
        print(f"{n}x  {to:40}  {key}")
    print(f"\n{sum(per_key.values())} email(s), {len(per_key)} distinct, {len(dupes)} duplicated")
    sys.exit(1 if dupes else 0)


if __name__ == "__main__":
    main()
