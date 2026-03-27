#!/usr/bin/env python3

import argparse
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import openreview
from tqdm import tqdm

# Environment variables can override defaults.
DEFAULT_VENUE_ID = os.environ.get("VENUE_ID", "NeurIPS.cc/2025/Conference")
VALID_DECISIONS = {"oral", "spotlight", "accepted", "rejected"}
REJECTED_SUFFIXES = ("Rejected_Submission", "Desk_Rejected")


def build_client() -> openreview.api.OpenReviewClient:
    """Return an OpenReview client, optionally authenticated via env vars."""
    username = os.environ.get("OPENREVIEW_USERNAME")
    password = os.environ.get("OPENREVIEW_PASSWORD")
    return openreview.api.OpenReviewClient(
        baseurl="https://api2.openreview.net",
        username=username,
        password=password,
    )


def conference_dir(venue_id: str) -> Path:
    """Pick a readable directory name from the venue id."""
    parts = venue_id.split("/")
    short_name = parts[0].split(".")[0] if parts else ""
    year = next((p for p in parts if p.isdigit()), "")
    if short_name and year:
        slug = f"{short_name}{year}".lower()
    else:
        slug = venue_id.replace("/", "_").lower()
    return Path("downloads") / slug


def sanitize_title(title: str) -> str:
    cleaned = "".join(c for c in title if c.isalnum() or c in " _-")
    cleaned = "_".join(cleaned.split())
    return cleaned[:120] or "paper"


def content_value(note, key: str) -> str:
    raw_value = note.content.get(key, "")
    if isinstance(raw_value, dict):
        raw_value = raw_value.get("value") or ""
    return str(raw_value) if raw_value else ""


def presentation_type(note) -> Optional[str]:
    """Return 'oral' or 'spotlight' if the note matches, else None."""
    venue_text = content_value(note, "venue").lower()
    decision_text = content_value(note, "decision").lower()
    combined = f"{venue_text} {decision_text}"
    if "oral" in combined:
        return "oral"
    if "spotlight" in combined:
        return "spotlight"
    return None


def note_decision(note, venue_id: str) -> Optional[str]:
    venueid = content_value(note, "venueid")
    label = presentation_type(note)

    if venueid == venue_id:
        return label or "accepted"

    lowered_vid = venueid.lower()
    if venueid.startswith(f"{venue_id}/") and (
        "reject" in lowered_vid or "desk" in lowered_vid
    ):
        return "rejected"

    combined_text = f"{content_value(note, 'venue')} {content_value(note, 'decision')}".lower()
    if "reject" in combined_text:
        return "rejected"

    return label


def supplementary_path(note, pdf_path: Path) -> Optional[Path]:
    supp_meta = note.content.get("supplementary_material", {})
    supp_value = supp_meta.get("value") if isinstance(supp_meta, dict) else None
    if not supp_value:
        return None
    ext = Path(supp_value).suffix or ".pdf"
    return pdf_path.with_name(pdf_path.stem + "_supp" + ext)


def paper_path(note, category: str, base_dir: Path) -> Path:
    title = content_value(note, "title")
    fname_parts = []
    if getattr(note, "number", None) is not None:
        fname_parts.append(f"{note.number:05d}")
    safe_title = sanitize_title(title)
    fname_parts.append(safe_title)
    fname = "_".join([p for p in fname_parts if p]) + ".pdf"
    return base_dir / category / fname


def parse_decisions(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    invalid = [p for p in parts if p not in VALID_DECISIONS]
    if invalid:
        raise argparse.ArgumentTypeError(
            f"Unknown decisions: {', '.join(sorted(set(invalid)))}."
        )
    ordered = []
    for part in parts:
        if part not in ordered:
            ordered.append(part)
    return ordered


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download OpenReview papers by decision."
    )
    parser.add_argument(
        "decisions",
        nargs="?",
        help="Comma-separated list of decisions to download (oral,spotlight,accepted,rejected).",
    )
    parser.add_argument(
        "--venue-id",
        default=DEFAULT_VENUE_ID,
        help="OpenReview venue id (default: NeurIPS 2025 Conference or env VENUE_ID).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory (default: downloads/<venue>/).",
    )
    parser.add_argument(
        "--no-skip-existing",
        dest="skip_existing",
        action="store_false",
        help="Re-download even if the file already exists.",
    )
    parser.add_argument(
        "--supplementary",
        action="store_true",
        help="Also download supplementary material for each paper if available.",
    )
    parser.add_argument(
        "--info",
        action="store_true",
        help="Print decision counts for the venue and exit.",
    )
    parser.set_defaults(skip_existing=True)

    args = parser.parse_args()
    try:
        parsed_decisions = parse_decisions(args.decisions)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))

    if not args.info and not parsed_decisions:
        parser.error("DECISIONS is required unless --info is provided.")

    args.decisions = parsed_decisions
    return args


def fetch_notes(
    client: openreview.api.OpenReviewClient, venue_id: str, need_rejected: bool
) -> Tuple[List, List]:
    accepted = client.get_all_notes(content={"venueid": venue_id})
    rejected: List = []
    if need_rejected:
        for suffix in REJECTED_SUFFIXES:
            rejected.extend(
                client.get_all_notes(content={"venueid": f"{venue_id}/{suffix}"})
            )
    return accepted, rejected


def decision_counts(accepted: Sequence, rejected: Sequence, venue_id: str) -> Dict[str, int]:
    counts = {key: 0 for key in VALID_DECISIONS}
    for note in accepted:
        label = note_decision(note, venue_id)
        if label == "oral":
            counts["oral"] += 1
            counts["accepted"] += 1
        elif label == "spotlight":
            counts["spotlight"] += 1
            counts["accepted"] += 1
        elif label == "accepted":
            counts["accepted"] += 1
    for note in rejected:
        if note_decision(note, venue_id) == "rejected":
            counts["rejected"] += 1
    return counts


def target_category(label: Optional[str], requested: set) -> Optional[str]:
    if label == "oral":
        if "oral" in requested:
            return "oral"
        if "accepted" in requested:
            return "accepted"
    elif label == "spotlight":
        if "spotlight" in requested:
            return "spotlight"
        if "accepted" in requested:
            return "accepted"
    elif label == "accepted":
        if "accepted" in requested:
            return "accepted"
    elif label == "rejected" and "rejected" in requested:
        return "rejected"
    return None


def collect_selected(
    accepted: Sequence,
    rejected: Sequence,
    venue_id: str,
    decisions: List[str],
    skip_existing: bool,
    base_dir: Path,
) -> Tuple[List[Tuple[object, str, Path]], int]:
    requested = set(decisions)
    selected = []
    existing = 0
    seen_ids = set()

    for note in accepted:
        label = note_decision(note, venue_id)
        target = target_category(label, requested)
        if not target or note.id in seen_ids:
            continue
        path = paper_path(note, target, base_dir)
        if skip_existing and path.exists():
            existing += 1
        else:
            selected.append((note, target, path))
        seen_ids.add(note.id)

    for note in rejected:
        target = target_category("rejected", requested)
        if not target or note.id in seen_ids:
            continue
        path = paper_path(note, target, base_dir)
        if skip_existing and path.exists():
            existing += 1
        else:
            selected.append((note, target, path))
        seen_ids.add(note.id)

    return selected, existing


def print_info(venue_id: str, counts: Dict[str, int]) -> None:
    parts = venue_id.split("/")
    short_name = parts[0].split(".")[0] if parts else venue_id
    year = next((p for p in parts if p.isdigit()), "")
    heading = " ".join(part for part in (short_name, year) if part)

    print(heading or venue_id)
    print("---")
    print(f"Oral: {counts['oral']}")
    print(f"Spotlight: {counts['spotlight']}")
    print(f"Accepted: {counts['accepted']}")
    print(f"Rejected: {counts['rejected']}")


def init_downloads_csv(
    csv_path: Path, venue_id: str, decisions: List[str],
    supplementary: bool, skip_existing: bool, out_dir: Path,
) -> None:
    now = datetime.now()
    with csv_path.open("a") as f:
        if csv_path.stat().st_size > 0:
            f.write("\n")
        f.write(f"{now.strftime('%Y-%m-%d')}\n")
        f.write(f"{now.strftime('%H:%M:%S')}\n")
        f.write(f"{venue_id}\n")
        f.write(f"{','.join(decisions)}\n")
        f.write(f"{supplementary}\n")
        f.write(f"{skip_existing}\n")
        f.write(f"{out_dir}\n")
        f.write("\ndownloaded:\n")


def append_download_row(csv_path: Path, filename: str, pdf_status: str, supp_status: str) -> None:
    with csv_path.open("a") as f:
        f.write(f"{filename},{pdf_status},{supp_status}\n")


def main() -> None:
    args = parse_args()

    client = build_client()
    base_dir = args.out_dir or conference_dir(args.venue_id)
    if not args.info:
        base_dir.mkdir(parents=True, exist_ok=True)
        csv_path = base_dir / "downloads.csv"
        init_downloads_csv(csv_path, args.venue_id, args.decisions, args.supplementary, args.skip_existing, base_dir)

    need_rejected = args.info or "rejected" in args.decisions
    print(f"Fetching accepted submissions for {args.venue_id}...")
    accepted, rejected = fetch_notes(client, args.venue_id, need_rejected)
    print(f"Accepted submissions: {len(accepted)}")
    if need_rejected:
        print(f"Rejected submissions: {len(rejected)}")

    counts = decision_counts(accepted, rejected, args.venue_id)
    if args.info:
        print_info(args.venue_id, counts)
        return

    to_download, already_present = collect_selected(
        accepted=accepted,
        rejected=rejected,
        venue_id=args.venue_id,
        decisions=args.decisions,
        skip_existing=args.skip_existing,
        base_dir=base_dir,
    )
    print(f"Requested decisions: {', '.join(args.decisions)}")
    print(f"Already present: {already_present}. To download now: {len(to_download)}")

    if args.supplementary:
        to_download = [(note, cat, path) for note, cat, path in to_download
                       if supplementary_path(note, path) is not None]
        print(f"Papers with supplementary material: {len(to_download)}")

    for note, category, path in tqdm(to_download, desc="Downloading", unit="paper"):
        pdf_meta = note.content.get("pdf", {})
        pdf_field_value = pdf_meta.get("value") if isinstance(pdf_meta, dict) else None
        if not pdf_field_value:
            tqdm.write(f"Skipping {note.id}: no pdf field")
            continue

        pdf_status = "failed"
        try:
            pdf_bytes = client.get_attachment(field_name="pdf", id=note.id)
        except Exception as exc:  # noqa: BLE001
            tqdm.write(f"Failed to fetch {note.id}: {exc}")
            append_download_row(csv_path, path.name, pdf_status, "N/A")
            continue

        try:
            tmp_path = path.with_suffix(path.suffix + ".part")
            tmp_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_bytes(pdf_bytes)
            tmp_path.replace(path)
            pdf_status = "ok"
        except Exception as exc:  # noqa: BLE001
            tqdm.write(f"Failed to save {path}: {exc}")
            append_download_row(csv_path, path.name, pdf_status, "N/A")
            continue

        supp_status = "N/A"
        if args.supplementary:
            supp_p = supplementary_path(note, path)
            if supp_p is not None and not (args.skip_existing and supp_p.exists()):
                try:
                    supp_bytes = client.get_attachment(field_name="supplementary_material", id=note.id)
                    tmp = supp_p.with_suffix(supp_p.suffix + ".part")
                    tmp.write_bytes(supp_bytes)
                    tmp.replace(supp_p)
                    supp_status = "ok"
                except Exception as exc:  # noqa: BLE001
                    tqdm.write(f"Failed to fetch/save supplementary for {note.id}: {exc}")
                    supp_status = "failed"

        append_download_row(csv_path, path.name, pdf_status, supp_status)

    print(f"Done. Files saved under {base_dir}/<decision>/")


if __name__ == "__main__":
    main()
