"""Command-line admin tool. Alternative to the Flask web UI.

Usage:
    python -m modules.admin_cli upload <file> --exam TNPSC --subject History --topic "Indian Freedom"
    python -m modules.admin_cli commit <upload_id>
    python -m modules.admin_cli discard <upload_id>
    python -m modules.admin_cli list-uploads [--status pending|committed|discarded]
    python -m modules.admin_cli delete-source <filename>
    python -m modules.admin_cli bulk-questions <csv_or_xlsx_or_json>
    python -m modules.admin_cli flag <question_id> [--reason ...]
    python -m modules.admin_cli resolve-flag <question_id>
    python -m modules.admin_cli list-flags
    python -m modules.admin_cli report
"""

import argparse
import getpass
import json
import logging
import sys

from config import settings
from modules.admin import AdminManager

logger = logging.getLogger(__name__)


def _authenticate() -> bool:
    cfg = settings.admin
    u = input(f"Admin username [{cfg.username}]: ").strip() or cfg.username
    p = getpass.getpass("Admin password: ")
    return u == cfg.username and p == cfg.password


def main(argv=None):
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(prog="admin_cli")
    parser.add_argument("--no-auth", action="store_true",
                        help="Skip interactive password prompt (dev only)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_up = sub.add_parser("upload", help="Upload and preview a document")
    p_up.add_argument("file")
    p_up.add_argument("--exam", default=settings.exam.default_exam)
    p_up.add_argument("--subject", default="General")
    p_up.add_argument("--topic", default="General")
    p_up.add_argument("--language", default="en")

    p_commit = sub.add_parser("commit")
    p_commit.add_argument("upload_id")

    p_discard = sub.add_parser("discard")
    p_discard.add_argument("upload_id")

    p_list = sub.add_parser("list-uploads")
    p_list.add_argument("--status", default=None)

    p_del_src = sub.add_parser("delete-source")
    p_del_src.add_argument("source")

    p_bulk = sub.add_parser("bulk-questions")
    p_bulk.add_argument("file")

    p_flag = sub.add_parser("flag")
    p_flag.add_argument("question_id")
    p_flag.add_argument("--reason", default="flagged via CLI")

    p_res = sub.add_parser("resolve-flag")
    p_res.add_argument("question_id")

    sub.add_parser("list-flags")
    sub.add_parser("report")
    sub.add_parser("duplicates")

    args = parser.parse_args(argv)

    if not args.no_auth and not _authenticate():
        print("Authentication failed.", file=sys.stderr)
        return 1

    admin = AdminManager()

    if args.cmd == "upload":
        preview = admin.upload_document(
            args.file,
            {"exam_type": args.exam, "subject": args.subject,
             "topic": args.topic, "language": args.language},
        )
        print(f"Upload ID: {preview.upload_id}")
        print(f"File: {preview.filename} | pages={preview.page_count} "
              f"chunks={preview.chunk_count} ocr={preview.ocr_used} "
              f"({preview.ocr_confidence}) tamil_valid={preview.tamil_valid}")
        if preview.duplicate_of:
            print(f"!! Duplicate of: {preview.duplicate_of}")
        for c in preview.chunks[:3]:
            print(f"\n[chunk {c.index} · {c.token_count} tokens]")
            print(c.text[:300] + ("..." if len(c.text) > 300 else ""))
        print(f"\nTo commit:  python -m modules.admin_cli commit {preview.upload_id}")
        return 0

    if args.cmd == "commit":
        res = admin.commit_upload(args.upload_id)
        print(f"Committed {res['chunks_committed']} chunks.")
        return 0

    if args.cmd == "discard":
        print("Discarded." if admin.discard_upload(args.upload_id) else "Not found.")
        return 0

    if args.cmd == "list-uploads":
        for u in admin.list_uploads(status=args.status):
            print(f"{u['upload_id'][:8]}  {u['status']:<10}  {u['filename']}  "
                  f"{u['exam_type']}/{u['subject']}/{u['topic']}  chunks={u['chunk_count']}")
        return 0

    if args.cmd == "delete-source":
        res = admin.manage_content("delete_source", source=args.source)
        print(f"Removed {res['removed']} chunks from {args.source}.")
        return 0

    if args.cmd == "bulk-questions":
        res = admin.bulk_upload_questions(args.file)
        print(f"Inserted: {res.inserted}, Skipped: {res.skipped}")
        for err in res.errors[:10]:
            print(f"  - {err}")
        return 0

    if args.cmd == "flag":
        admin.flag_question(args.question_id, reason=args.reason)
        print("Flagged.")
        return 0

    if args.cmd == "resolve-flag":
        print("Resolved." if admin.resolve_flag(args.question_id) else "Not found.")
        return 0

    if args.cmd == "list-flags":
        for fl in admin.list_flagged_questions():
            print(f"{fl['question_id'][:8]}  {fl['flagged_at']}  {fl['reason']}")
        return 0

    if args.cmd == "duplicates":
        dups = admin.find_duplicate_questions()
        for a, b in dups:
            print(f"{a[:8]}  <->  {b[:8]}")
        print(f"\n{len(dups)} duplicate pair(s)")
        return 0

    if args.cmd == "report":
        print(json.dumps(admin.generate_content_report(), indent=2))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
