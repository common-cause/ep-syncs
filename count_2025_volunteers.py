"""
Quick count of EP volunteers who completed shifts in 2025.

Pulls shift_volunteers_csv for all 50 states + DC, filters to 2025,
and counts unique volunteers by email. Run from the ep-syncs directory
with the .env file present.

Usage:
    python count_2025_volunteers.py
"""

import sys
from collections import defaultdict

from dotenv import load_dotenv
load_dotenv()

from ccef_connections import PTVConnector

ALL_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
]


def main():
    print("Connecting to PTV...")
    with PTVConnector() as ptv:
        print(f"Pulling shift_volunteers for {len(ALL_STATES)} states...\n")
        all_rows = ptv.get_all_shift_volunteers(ALL_STATES)

    print(f"\nTotal rows fetched (all years): {len(all_rows)}")

    # Filter to 2025
    rows_2025 = [r for r in all_rows if str(r.get("date", "")).startswith("2025")]
    print(f"Rows in 2025: {len(rows_2025)}")

    if not rows_2025:
        print("\nNo 2025 shift data found.")
        sys.exit(0)

    # Count by state
    state_counts = defaultdict(set)  # state -> set of emails
    no_email = 0
    for row in rows_2025:
        email = (row.get("email") or "").strip().lower()
        state = row.get("state", "??")
        if email:
            state_counts[state].add(email)
        else:
            no_email += 1

    # Unique volunteers across all states (deduplicated by email)
    all_emails = set()
    for emails in state_counts.values():
        all_emails.update(emails)

    # Print breakdown
    print("\n--- 2025 Volunteer Counts by State (unique emails per state) ---")
    for state in sorted(state_counts.keys()):
        print(f"  {state}: {len(state_counts[state]):,}")

    print("\n--- Summary ---")
    print(f"  States with 2025 data: {len(state_counts)}")
    print(f"  Total shift-volunteer rows (2025): {len(rows_2025):,}")
    if no_email:
        print(f"  Rows missing email: {no_email:,}")
    print(f"\n  UNIQUE VOLUNTEERS (deduplicated across all states): {len(all_emails):,}")


if __name__ == "__main__":
    main()
