# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""
pb_renderman package: Physically Based -> PxrSurface for Maya + RenderMan.
Author: Julien Duval. See pb_renderman.py and README.txt in this folder.

Usage (Script Editor, Python tab, or a shelf button):
    import pb_renderman
    pb_renderman.show()

show() reloads the tool each time, so an updated pb_renderman.py is picked up
without restarting Maya.
"""

import importlib

from . import pb_renderman as core

_window = None


def show(reload=True):
    """Opens the window (closing the previous one), reloading the tool first."""
    global core, _window
    try:
        _window.close()
        _window.deleteLater()
    except Exception:
        pass
    if reload:
        core = importlib.reload(core)
    _window = core.show()
    return _window


def __getattr__(name):
    # pb_renderman.apply_by_name(...), pb_renderman.__version__, ... come from the tool module
    return getattr(core, name)
