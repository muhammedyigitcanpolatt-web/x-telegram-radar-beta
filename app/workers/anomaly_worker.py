"""Compatibility command for the supported Redis analyzer worker."""

from app.workers.analyzer import main, run_analyzer_worker


if __name__ == "__main__":
    main()
