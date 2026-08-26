"""`python -m saipet.gui` entrypoint. Importing this module builds nothing;
the window appears only when the module runs as __main__."""

from saipet.gui.app import run

if __name__ == "__main__":
    run()
