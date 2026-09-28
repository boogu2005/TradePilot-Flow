from diagnostics.cli import build_parser


def test_cli_accepts_all_review_decisions():
    parser = build_parser()
    for decision in ("approve", "modify", "reject", "request_information"):
        args = parser.parse_args(["review", "incident-1", "--decision", decision, "--reviewer", "alice"])
        assert args.decision == decision
