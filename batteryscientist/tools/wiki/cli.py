"""Command-line entry point for Wiki maintenance."""

import argparse
from pathlib import Path
import sys

from .storage import (
    _CLAIM_STATUSES,
    _IDEA_OUTCOMES,
    _normalize_arxiv_id,
    add_claim,
    add_edge,
    add_experiment,
    append_log,
    get_stats,
    ingest_paper,
    init_wiki,
    rebuild_index,
    slugify,
    sync_papers,
    upsert_idea,
)


def main():
    parser = argparse.ArgumentParser(description="ARIS Research Wiki utilities")
    subparsers = parser.add_subparsers(dest="command")

    # init
    p_init = subparsers.add_parser("init")
    p_init.add_argument("wiki_root")

    # slug
    p_slug = subparsers.add_parser("slug")
    p_slug.add_argument("title")
    p_slug.add_argument("--author", default="")
    p_slug.add_argument("--year", type=int, default=0)

    # add_edge
    p_edge = subparsers.add_parser("add_edge")
    p_edge.add_argument("wiki_root")
    p_edge.add_argument("--from", dest="from_id", required=True)
    p_edge.add_argument("--to", dest="to_id", required=True)
    p_edge.add_argument("--type", dest="edge_type", required=True)
    p_edge.add_argument("--evidence", default="")

    # rebuild_index
    p_idx = subparsers.add_parser("rebuild_index")
    p_idx.add_argument("wiki_root")

    # stats
    p_stats = subparsers.add_parser("stats")
    p_stats.add_argument("wiki_root")

    # log
    p_log = subparsers.add_parser("log")
    p_log.add_argument("wiki_root")
    p_log.add_argument("message")

    # ingest_paper — the canonical ingest entrypoint called by integration hooks
    p_ing = subparsers.add_parser("ingest_paper",
                                   help="Create (or update) a papers/<slug>.md page")
    p_ing.add_argument("wiki_root")
    p_ing.add_argument("--arxiv-id", default="",
                       help="arXiv identifier (2501.12345 or with v2); metadata auto-fetched")
    p_ing.add_argument("--title", default="",
                       help="Paper title; required when --arxiv-id is absent")
    p_ing.add_argument("--authors", default="",
                       help='Comma-separated author list, e.g. "Alice Smith, Bob Jones"')
    p_ing.add_argument("--year", type=int, default=0)
    p_ing.add_argument("--venue", default="")
    p_ing.add_argument("--external-id-doi", dest="doi", default="")
    p_ing.add_argument("--thesis", default="",
                       help="One-line thesis; otherwise left as TODO for later enrichment")
    p_ing.add_argument("--tags", default="",
                       help="Comma-separated tag list")
    p_ing.add_argument("--update-on-exist", action="store_true",
                       help="Overwrite an existing page instead of skipping (default: skip)")

    # add_claim — create a claim node (PROVE/JUDGE output ledger)
    p_claim = subparsers.add_parser("add_claim",
                                    help="Create (or update) a claims/<slug>.md node")
    p_claim.add_argument("wiki_root")
    p_claim.add_argument("--slug", default="",
                         help="Stable claim id, e.g. b1-main-ub (honored verbatim)")
    p_claim.add_argument("--name", required=True,
                         help="Human-readable claim name/headline")
    p_claim.add_argument("--description", default="",
                         help="One-line description for the index/frontmatter")
    p_claim.add_argument("--status", default="drafted",
                         help="One of: " + ", ".join(sorted(_CLAIM_STATUSES)))
    p_claim.add_argument("--provenance", default="",
                         help="Run directory that produced this claim (honesty receipt)")
    p_claim.add_argument("--statement", default="", help="Formal statement (body)")
    p_claim.add_argument("--scope", default="",
                         help="Honest scope: what the claim does NOT say / banned wordings")
    p_claim.add_argument("--evidence", default="",
                         help="Evidence chain: obligations, verdicts, provenance pointers")
    p_claim.add_argument("--tags", default="", help="Comma-separated tag list")
    p_claim.add_argument("--addresses", default="",
                         help="Comma-separated gap ids this claim addresses, e.g. G2,G10")
    p_claim.add_argument("--extends", default="",
                         help="Comma-separated paper node_ids/slugs this claim extends")
    p_claim.add_argument("--uses", default="",
                         help="Comma-separated paper node_ids/slugs this claim uses")
    p_claim.add_argument("--depends-on", dest="depends_on", default="",
                         help="Comma-separated claim node_ids/slugs this claim depends on")
    p_claim.add_argument("--refutes", default="",
                         help="Comma-separated claim node_ids/slugs this claim refutes")
    p_claim.add_argument("--update-on-exist", action="store_true",
                         help="Overwrite an existing claim instead of skipping (default: skip)")

    # upsert_idea — create/update an idea node (idea-creator Phase 7 write-back)
    p_idea = subparsers.add_parser("upsert_idea",
                                   help="Create (or update) an ideas/<slug>.md node")
    p_idea.add_argument("wiki_root")
    p_idea.add_argument("--slug", default="",
                        help="Stable idea id (honored verbatim); else derived from --title")
    p_idea.add_argument("--title", required=True, help="Human-readable idea title")
    p_idea.add_argument("--description", default="",
                        help="One-line description for the index/frontmatter")
    p_idea.add_argument("--stage", default="proposed",
                        help="proposed | active | piloted | archived")
    p_idea.add_argument("--outcome", default="pending",
                        help="One of: " + ", ".join(sorted(_IDEA_OUTCOMES)))
    p_idea.add_argument("--thesis", default="", help="Core hypothesis / direction (body)")
    p_idea.add_argument("--structure", default="",
                        help="Input/output formats, tensor axes, and model architecture (strcture body)")
    p_idea.add_argument("--tags", default="", help="Comma-separated tag list")
    p_idea.add_argument("--based-on", dest="based_on", default="",
                        help="Comma-separated paper node_ids/slugs that inspired this idea")
    p_idea.add_argument("--target-gaps", dest="target_gaps", default="",
                        help="Comma-separated gap ids this idea addresses, e.g. G2,G10")
    p_idea.add_argument("--update-on-exist", action="store_true",
                        help="Overwrite an existing idea instead of skipping (default: skip)")

    # add_experiment — create/update an experiment node (result-to-claim Step 5 #1)
    p_exp = subparsers.add_parser("add_experiment",
                                  help="Create (or update) an experiments/<slug>.md node")
    p_exp.add_argument("wiki_root")
    p_exp.add_argument("--slug", required=True, help="Stable experiment id, e.g. exp-001")
    p_exp.add_argument("--title", default="", help="Human-readable label (default: 'Experiment <slug>')")
    p_exp.add_argument("--idea", default="", help="Idea node_id/slug this experiment tests (wires idea --tested_by--> exp)")
    for field in ("question", "hypothesis", "protocol", "results"):
        p_exp.add_argument(f"--{field}", default="", help=f"Experiment {field} (body)")
    p_exp.add_argument("--tags", default="", help="Comma-separated tag list")
    p_exp.add_argument("--update-on-exist", action="store_true",
                       help="Overwrite an existing experiment instead of skipping")

    # sync — batch backfill
    p_sync = subparsers.add_parser("sync",
                                    help="Batch ingest from a list of arXiv IDs")
    p_sync.add_argument("wiki_root")
    p_sync.add_argument("--arxiv-ids", default="",
                        help="Comma-separated list of arXiv IDs")
    p_sync.add_argument("--from-file", default="",
                        help="Path to a newline-delimited file of arXiv IDs (# comments ok)")
    p_sync.add_argument("--update-on-exist", action="store_true")

    args = parser.parse_args()

    if args.command == "init":
        init_wiki(args.wiki_root, separate_origins=True)
    elif args.command == "slug":
        print(slugify(args.title, args.author, args.year))
    elif args.command == "add_edge":
        add_edge(args.wiki_root, args.from_id, args.to_id, args.edge_type, args.evidence)
    elif args.command == "rebuild_index":
        rebuild_index(args.wiki_root)
    elif args.command == "stats":
        get_stats(args.wiki_root)
    elif args.command == "log":
        append_log(args.wiki_root, args.message)
    elif args.command == "ingest_paper":
        authors = [a.strip() for a in args.authors.split(",") if a.strip()]
        tags = [t.strip() for t in args.tags.split(",") if t.strip()]
        ingest_paper(args.wiki_root,
                     arxiv_id=args.arxiv_id, title=args.title,
                     authors=authors, year=args.year, venue=args.venue,
                     doi=args.doi, thesis=args.thesis, tags=tags,
                     update_on_exist=args.update_on_exist)
    elif args.command == "add_claim":
        def _split(s: str) -> list[str]:
            return [x.strip() for x in s.split(",") if x.strip()]
        add_claim(args.wiki_root, args.slug, args.name,
                  description=args.description, status=args.status,
                  provenance=args.provenance, statement=args.statement,
                  scope=args.scope, evidence=args.evidence,
                  tags=_split(args.tags),
                  addresses=_split(args.addresses), extends=_split(args.extends),
                  uses=_split(args.uses), depends_on=_split(args.depends_on),
                  refutes=_split(args.refutes),
                  update_on_exist=args.update_on_exist)
    elif args.command == "upsert_idea":
        def _spliti(s: str) -> list[str]:
            return [x.strip() for x in s.split(",") if x.strip()]
        upsert_idea(args.wiki_root, args.slug, args.title,
                    description=args.description, stage=args.stage, outcome=args.outcome,
                    thesis=args.thesis, structure=args.structure, tags=_spliti(args.tags),
                    based_on=_spliti(args.based_on), target_gaps=_spliti(args.target_gaps),
                    update_on_exist=args.update_on_exist)
    elif args.command == "add_experiment":
        add_experiment(args.wiki_root, args.slug, title=args.title, idea=args.idea,
                       question=args.question, hypothesis=args.hypothesis,
                       protocol=args.protocol, results=args.results,
                       tags=[x.strip() for x in args.tags.split(",") if x.strip()],
                       update_on_exist=args.update_on_exist)
    elif args.command == "sync":
        ids: list[str] = []
        if args.arxiv_ids:
            ids.extend([i.strip() for i in args.arxiv_ids.split(",") if i.strip()])
        if args.from_file:
            fp = Path(args.from_file)
            if not fp.exists():
                print(f"--from-file not found: {fp}", file=sys.stderr)
                sys.exit(2)
            for line in fp.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    ids.append(line)
        if not ids:
            print("sync: no arxiv ids supplied (use --arxiv-ids or --from-file)",
                  file=sys.stderr)
            sys.exit(2)
        # Dedup the id list before we hit the network
        seen: set[str] = set()
        uniq_ids: list[str] = []
        for i in ids:
            key = _normalize_arxiv_id(i)
            if key in seen:
                continue
            seen.add(key)
            uniq_ids.append(i)
        print(f"sync: {len(uniq_ids)} unique arxiv id(s)")
        sync_papers(args.wiki_root, uniq_ids, update_on_exist=args.update_on_exist)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
