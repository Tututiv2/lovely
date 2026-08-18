"""Lovely -- a Minecraft: Java Edition launcher with per-version isolation.

The core (auth, resolve, download, launch) is a library with **no UI imports**, so all of
it is drivable headlessly by tests. That is the one architectural decision the rest of the
project leans on: almost none of the hard parts are visual.

Import order of interest:

* :mod:`launcher.versions`  -- manifest and the ``inheritsFrom`` merge
* :mod:`launcher.libraries` -- rules, classpath, natives
* :mod:`launcher.assets`    -- the shared, content-addressed asset store
* :mod:`launcher.java`      -- runtimes the launcher downloads itself
* :mod:`launcher.instances` -- isolation
* :mod:`launcher.launch`    -- argument templating and the process
"""

__all__ = ["paths", "logs", "net", "rules", "versions", "libraries", "assets",
           "java", "instances", "launch", "settings", "accounts", "loaders", "nbt"]
