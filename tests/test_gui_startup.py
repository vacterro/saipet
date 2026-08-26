"""Startup contract: importing the GUI opens nothing; the window appears only
when the entrypoint runs."""


def test_importing_saipet_gui_opens_no_window():
    import tkinter

    import saipet.gui

    assert saipet.gui is not None
    assert not tkinter._default_root


def test_importing_the_app_module_opens_no_window():
    import tkinter

    import saipet.gui.app

    assert saipet.gui.app is not None
    assert not tkinter._default_root


def test_importing_the_entrypoint_does_not_run_it():
    """`python -m saipet.gui` runs app.run(); a plain import must not."""
    import tkinter

    import saipet.gui.__main__  # noqa: F401  -- guarded by __name__

    assert not tkinter._default_root
