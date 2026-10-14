"""Compatibility entrypoint for the workflow-backed read-only monitor.

New live runs require --live-run and select exactly 20 unsolved challenges.
Duration limits end observation; this command never stops or repairs a run.
"""

if __package__:
    from .monitor_real_competition import main
else:
    from monitor_real_competition import main


if __name__ == "__main__":
    raise SystemExit(main())
