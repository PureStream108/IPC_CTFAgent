if __package__:
    from .monitor_real_competition import main
else:
    from monitor_real_competition import main


if __name__ == "__main__":
    raise SystemExit(main())
